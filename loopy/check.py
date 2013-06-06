from __future__ import division

__copyright__ = "Copyright (C) 2012 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""


from islpy import dim_type
import islpy as isl
from loopy.symbolic import WalkMapper


# {{{ sanity checks run during scheduling

def check_for_unused_hw_axes_in_insns(kernel):
    group_size, local_size = kernel.get_grid_sizes_as_exprs()

    group_axes = set(ax for ax, length in enumerate(group_size))
    local_axes = set(ax for ax, length in enumerate(local_size))

    # alternative: just disregard length-1 dimensions?

    from loopy.kernel.data import LocalIndexTag, AutoLocalIndexTagBase, GroupIndexTag
    for insn in kernel.instructions:
        if insn.boostable:
            continue

        group_axes_used = set()
        local_axes_used = set()

        for iname in kernel.insn_inames(insn):
            tag = kernel.iname_to_tag.get(iname)

            if isinstance(tag, LocalIndexTag):
                local_axes_used.add(tag.axis)
            elif isinstance(tag, GroupIndexTag):
                group_axes_used.add(tag.axis)
            elif isinstance(tag, AutoLocalIndexTagBase):
                raise RuntimeError("auto local tag encountered")

        if group_axes != group_axes_used:
            raise RuntimeError("instruction '%s' does not use all group hw axes "
                    "(available: %s used:%s)"
                    % (insn.id,
                        ",".join(str(i) for i in group_axes),
                        ",".join(str(i) for i in group_axes_used)))
        if local_axes != local_axes_used:
            raise RuntimeError("instruction '%s' does not use all local hw axes"
                    "(available: %s used:%s)"
                    % (insn.id,
                        ",".join(str(i) for i in local_axes),
                        ",".join(str(i) for i in local_axes_used)))


def check_for_double_use_of_hw_axes(kernel):
    from loopy.kernel.data import UniqueTag

    for insn in kernel.instructions:
        insn_tag_keys = set()
        for iname in kernel.insn_inames(insn):
            tag = kernel.iname_to_tag.get(iname)
            if isinstance(tag, UniqueTag):
                key = tag.key
                if key in insn_tag_keys:
                    raise RuntimeError("instruction '%s' has multiple "
                            "inames tagged '%s'" % (insn.id, tag))

                insn_tag_keys.add(key)


def check_for_inactive_iname_access(kernel):
    from loopy.symbolic import DependencyMapper
    depmap = DependencyMapper()

    for insn in kernel.instructions:
        expression_indices = depmap(insn.expression)
        expression_inames = expression_indices & kernel.all_inames()

        if not expression_inames <= kernel.insn_inames(insn):
            raise RuntimeError(
                    "instructiosn '%s' references "
                    "inames that the instruction does not depend on"
                    % insn.id)


class WriteRaceConditionError(RuntimeError):
    pass


def check_for_write_races(kernel):
    from loopy.symbolic import DependencyMapper
    from loopy.kernel.data import ParallelTag, GroupIndexTag, LocalIndexTagBase
    depmap = DependencyMapper()

    iname_to_tag = kernel.iname_to_tag.get
    for insn in kernel.instructions:
        assignee_name = insn.get_assignee_var_name()
        assignee_indices = depmap(insn.get_assignee_indices())

        def strip_var(expr):
            from pymbolic.primitives import Variable
            assert isinstance(expr, Variable)
            return expr.name

        assignee_indices = set(strip_var(index) for index in assignee_indices)

        assignee_inames = assignee_indices & kernel.all_inames()
        if not assignee_inames <= kernel.insn_inames(insn):
            raise RuntimeError(
                    "assignee of instructiosn '%s' references "
                    "iname that the instruction does not depend on"
                    % insn.id)

        if assignee_name in kernel.arg_dict:
            # Any parallel tags that are not depended upon by the assignee
            # will cause write races.

            raceable_parallel_insn_inames = set(
                    iname
                    for iname in kernel.insn_inames(insn)
                    if isinstance(iname_to_tag(iname), ParallelTag))

        elif assignee_name in kernel.temporary_variables:
            temp_var = kernel.temporary_variables[assignee_name]
            if temp_var.is_local is True:
                raceable_parallel_insn_inames = set(
                        iname
                        for iname in kernel.insn_inames(insn)
                        if isinstance(iname_to_tag(iname), ParallelTag)
                        and not isinstance(iname_to_tag(iname), GroupIndexTag))

            elif temp_var.is_local is False:
                raceable_parallel_insn_inames = set(
                        iname
                        for iname in kernel.insn_inames(insn)
                        if isinstance(iname_to_tag(iname), ParallelTag)
                        and not isinstance(iname_to_tag(iname),
                            GroupIndexTag)
                        and not isinstance(iname_to_tag(iname),
                            LocalIndexTagBase))

            else:
                raise RuntimeError("temp var '%s' hasn't decided on "
                        "whether it is local" % temp_var.name)

        else:
            raise RuntimeError("invalid assignee name in instruction '%s'"
                    % insn.id)

        race_inames = \
                raceable_parallel_insn_inames - assignee_inames

        if race_inames:
            raise WriteRaceConditionError(
                    "instruction '%s' contains a write race: "
                    "instruction will be run across parallel iname(s) '%s', which "
                    "is/are not referenced in the lhs index"
                    % (insn.id, ",".join(race_inames)))


def check_for_orphaned_user_hardware_axes(kernel):
    from loopy.kernel.data import LocalIndexTag
    for axis in kernel.local_sizes:
        found = False
        for tag in kernel.iname_to_tag.itervalues():
            if isinstance(tag, LocalIndexTag) and tag.axis == axis:
                found = True
                break

        if not found:
            raise RuntimeError("user-requested local hardware axis %d "
                    "has no iname mapped to it" % axis)


def check_for_data_dependent_parallel_bounds(kernel):
    from loopy.kernel.data import ParallelTag

    for i, dom in enumerate(kernel.domains):
        dom_inames = set(dom.get_var_names(dim_type.set))
        par_inames = set(iname
                for iname in dom_inames
                if isinstance(kernel.iname_to_tag.get(iname), ParallelTag))

        if not par_inames:
            continue

        parameters = set(dom.get_var_names(dim_type.param))
        for par in parameters:
            if par in kernel.temporary_variables:
                raise RuntimeError("Domain number %d has a data-dependent "
                        "parameter '%s' and contains parallel "
                        "inames '%s'. This is not allowed (for now)."
                        % (i, par, ", ".join(par_inames)))


class _AccessCheckMapper(WalkMapper):
    def __init__(self, kernel, domain, insn_id):
        self.kernel = kernel
        self.domain = domain
        self.insn_id = insn_id

    def map_subscript(self, expr):
        WalkMapper.map_subscript(self, expr)

        from pymbolic.primitives import Variable
        assert isinstance(expr.aggregate, Variable)

        shape = None
        var_name = expr.aggregate.name
        if var_name in self.kernel.arg_dict:
            arg = self.kernel.arg_dict[var_name]
            shape = arg.shape
        elif var_name in self.kernel.temporary_variables:
            tv = self.kernel.temporary_variables[var_name]
            shape = tv.shape

        if shape is not None:
            subscript = expr.index

            if not isinstance(subscript, tuple):
                subscript = (subscript,)

            from loopy.symbolic import get_dependencies, get_access_range

            available_vars = set(self.domain.get_var_dict())
            if not (get_dependencies(subscript) <= available_vars
                    and get_dependencies(shape) <= available_vars):
                return

            if len(subscript) != len(shape):
                raise RuntimeError("subscript to '%s' in '%s' has the wrong "
                        "number of indices (got: %d, expected: %d)" % (
                            expr.aggregate.name, expr,
                            len(subscript), len(shape)))

            try:
                access_range = get_access_range(self.domain, subscript)
            except isl.Error:
                # Likely: index was non-linear, nothing we can do.
                return

            shape_domain = isl.BasicSet.universe(access_range.get_space())
            for idim in xrange(len(subscript)):
                from loopy.isl_helpers import make_slab
                slab = make_slab(
                        shape_domain.get_space(), (dim_type.in_, idim),
                        0, shape[idim])

                shape_domain = shape_domain.intersect(slab)

            if not access_range.is_subset(shape_domain):
                raise RuntimeError("'%s' in instruction '%s' "
                        "accesses out-of-bounds array element"
                        % (expr, self.insn_id))


def check_bounds(kernel):
    temp_var_names = set(kernel.temporary_variables)
    for insn in kernel.instructions:
        domain = kernel.get_inames_domain(kernel.insn_inames(insn))

        # data-dependent bounds? can't do much
        if set(domain.get_var_names(dim_type.param)) & temp_var_names:
            continue

        acm = _AccessCheckMapper(kernel, domain, insn.id)
        acm(insn.expression)
        acm(insn.assignee)


def check_write_destinations(kernel):
    for insn in kernel.instructions:
        wvar = insn.get_assignee_var_name()

        if wvar in kernel.all_inames():
            raise RuntimeError("iname '%s' may not be written" % wvar)

        insn_domain = kernel.get_inames_domain(kernel.insn_inames(insn))
        insn_params = set(insn_domain.get_var_names(dim_type.param))

        if wvar in kernel.all_params():
            if wvar not in kernel.temporary_variables:
                raise RuntimeError("domain parameter '%s' may not be written"
                        "--it is not a temporary variable" % wvar)

            if wvar in insn_params:
                raise RuntimeError("domain parameter '%s' may not be written "
                        "inside a domain dependent on it" % wvar)

        if not (wvar in kernel.temporary_variables
                or wvar in kernel.arg_dict) and wvar not in kernel.all_params():
            raise RuntimeError

# }}}


def run_automatic_checks(kernel):
    try:
        check_for_orphaned_user_hardware_axes(kernel)
        check_for_double_use_of_hw_axes(kernel)
        check_for_unused_hw_axes_in_insns(kernel)
        check_for_inactive_iname_access(kernel)
        check_for_write_races(kernel)
        check_for_data_dependent_parallel_bounds(kernel)
        check_bounds(kernel)
        check_write_destinations(kernel)
    except:
        print 75*"="
        print "failing kernel after processing:"
        print 75*"="
        print kernel
        print 75*"="
        raise


# {{{ sanity-check for implemented domains of each instruction

def check_implemented_domains(kernel, implemented_domains, code=None):
    from islpy import dim_type

    from islpy import align_two

    for insn_id, idomains in implemented_domains.iteritems():
        insn = kernel.id_to_insn[insn_id]

        assert idomains

        insn_impl_domain = idomains[0]
        for idomain in idomains[1:]:
            insn_impl_domain = insn_impl_domain | idomain
        assumption_non_param = isl.BasicSet.from_params(kernel.assumptions)
        assumptions, insn_impl_domain = align_two(
                assumption_non_param, insn_impl_domain)
        insn_impl_domain = (
                (insn_impl_domain & assumptions)
                .project_out_except(kernel.insn_inames(insn), [dim_type.set]))

        insn_inames = kernel.insn_inames(insn)
        insn_domain = kernel.get_inames_domain(insn_inames)
        assumptions, insn_domain = align_two(assumption_non_param, insn_domain)
        desired_domain = ((insn_domain & assumptions)
            .project_out_except(kernel.insn_inames(insn), [dim_type.set]))

        insn_impl_domain, desired_domain = align_two(
                insn_impl_domain, desired_domain)

        if insn_impl_domain != desired_domain:
            i_minus_d = insn_impl_domain - desired_domain
            d_minus_i = desired_domain - insn_impl_domain

            parameter_inames = set(
                    insn_domain.get_dim_name(dim_type.param, i)
                    for i in range(insn_domain.dim(dim_type.param)))

            lines = []
            for kind, diff_set in [
                    ("implemented, but not desired", i_minus_d),
                    ("desired, but not implemented", d_minus_i)]:
                diff_set = diff_set.coalesce()
                pt = diff_set.sample_point()
                if pt.is_void():
                    continue

                #pt_set = isl.Set.from_point(pt)
                #lines.append("point implemented: %s" % (pt_set <= insn_impl_domain))
                #lines.append("point desired: %s" % (pt_set <= desired_domain))

                iname_to_dim = pt.get_space().get_var_dict()
                point_axes = []
                for iname in kernel.insn_inames(insn) | parameter_inames:
                    tp, dim = iname_to_dim[iname]
                    point_axes.append("%s=%d" % (iname, pt.get_coordinate(tp, dim)))

                lines.append(
                        "sample point %s: %s" % (kind, ", ".join(point_axes)))

            if code is not None:
                print 79*"-"
                print "CODE:"
                print 79*"-"
                from loopy.compiled import get_highlighted_code
                print get_highlighted_code(code)
                print 79*"-"

            raise RuntimeError("sanity check failed--implemented and desired "
                    "domain for instruction '%s' do not match\n\n"
                    "implemented: %s\n\n"
                    "desired:%s\n\n%s"
                    % (insn_id, insn_impl_domain, desired_domain, "\n".join(lines)))

    # placate the assert at the call site
    return True

# }}}


# {{{ user-invoked checks

def get_problems(kernel, parameters):
    """
    :return: *(max_severity, list of (severity, msg))*, where *severity*
        ranges from 1-5.  '5' means 'will certainly not run'.
    """
    msgs = []

    def msg(severity, s):
        msgs.append((severity, s))

    glens, llens = kernel.get_grid_sizes_as_exprs()

    if (max(len(glens), len(llens))
            > kernel.device.max_work_item_dimensions):
        msg(5, "too many work item dimensions")

    from pymbolic import evaluate
    from pymbolic.mapper.evaluator import UnknownVariableError
    try:
        glens = evaluate(glens, parameters)
        llens = evaluate(llens, parameters)
    except UnknownVariableError, name:
        msg(1, "could not check axis bounds because no value "
                "for variable '%s' was passed to check_kernels()"
                % name)
    else:
        for i in range(len(llens)):
            if llens[i] > kernel.device.max_work_item_sizes[i]:
                msg(5, "group axis %d too big" % i)

        from pytools import product
        if product(llens) > kernel.device.max_work_group_size:
            msg(5, "work group too big")

    import pyopencl as cl
    from pyopencl.characterize import usable_local_mem_size
    if kernel.local_mem_use() > usable_local_mem_size(kernel.device):
        if kernel.device.local_mem_type == cl.device_local_mem_type.LOCAL:
            msg(5, "using too much local memory")
        else:
            msg(4, "using more local memory than available--"
                    "possibly OK due to cache nature")

    from loopy.kernel.data import ConstantArg
    const_arg_count = sum(
            1 for arg in kernel.args
            if isinstance(arg, ConstantArg))

    if const_arg_count > kernel.device.max_constant_args:
        msg(5, "too many constant arguments")

    max_severity = 0
    for sev, msg in msgs:
        max_severity = max(sev, max_severity)
    return max_severity, msgs


def check_kernels(kernel_gen, parameters={}, kill_level_min=5,
        warn_level_min=1):
    for kernel in kernel_gen:
        max_severity, msgs = get_problems(kernel, parameters)

        for severity, msg in msgs:
            if severity >= warn_level_min:
                from warnings import warn
                from loopy import LoopyAdvisory
                warn(msg, LoopyAdvisory)

        if max_severity < kill_level_min:
            yield kernel

# }}}

# vim: foldmethod=marker
