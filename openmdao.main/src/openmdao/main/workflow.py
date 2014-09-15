""" Base class for all workflows. """

from fnmatch import fnmatch
from math import isnan
import sys
from traceback import format_exc
import weakref
from StringIO import StringIO

from numpy import ndarray

# pylint: disable-msg=E0611,F0401
from openmdao.main.case import Case
from openmdao.main.mpiwrap import MPI, MPI_info, mpiprint
from openmdao.main.systems import SerialSystem, ParallelSystem, \
                                  partition_subsystems, ParamSystem, \
                                  get_comm_if_active#, _create_simple_sys
from openmdao.main.depgraph import _get_inner_connections, reduced2component, \
                                   simple_node_iter
from openmdao.main.exceptions import RunStopped, TracedError
from openmdao.main.interfaces import IVariableTree

__all__ = ['Workflow']


def _flattened_names(name, val, names=None):
    """ Return list of names for values in `val`.
    Note that this expands arrays into an entry for each index!.
    """
    if names is None:
        names = []
    if isinstance(val, float):
        names.append(name)
    elif isinstance(val, ndarray):
        for i in range(len(val)):
            value = val[i]
            _flattened_names('%s[%s]' % (name, i), value, names)
    elif IVariableTree.providedBy(val):
        for key in sorted(val.list_vars()):  # Force repeatable order.
            value = getattr(val, key)
            _flattened_names('.'.join((name, key)), value, names)
    else:
        raise TypeError('Variable %s is of type %s which is not convertable'
                        ' to a 1D float array.' % (name, type(val)))
    return names


class Workflow(object):
    """
    A Workflow consists of a collection of Components which are to be executed
    in some order during a single iteration of a Driver.
    """

    def __init__(self, parent, members=None):
        """Create a Workflow.

        parent: Driver
            The Driver that contains this Workflow.  This option is normally
            passed instead of scope because scope usually isn't known at
            initialization time.  If scope is not provided, it will be
            set to parent.parent, which should be the Assembly that contains
            the parent Driver.

        members: list of str (optional)
            A list of names of Components to add to this workflow.
        """
        self._parent = None
        self.parent = parent
        self._stop = False
        self._scope = None
        self._exec_count = 0     # Workflow executions since reset.
        self._initial_count = 0  # Value to reset to (typically zero).
        self._comp_count = 0     # Component index in workflow.
        self._wf_comp_graph = None
        self._var_graph = None
        self._system = None

        self._rec_required = None  # Case recording configuration.
        self._rec_parameters = None
        self._rec_objectives = None
        self._rec_responses = None
        self._rec_constraints = None
        self._rec_outputs = None

        if members:
            for member in members:
                if not isinstance(member, basestring):
                    raise TypeError("Components must be added to a workflow by name.")
                self.add(member)

        self.mpi = MPI_info()

    def __getstate__(self):
        state = self.__dict__.copy()
        state['_parent'] = None if self._parent is None else self._parent()
        state['_scope'] = None if self._scope is None else self._scope()
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.parent = state['_parent']
        self.scope = state['_scope']

    @property
    def parent(self):
        """ This workflow's driver. """
        return None if self._parent is None else self._parent()

    @parent.setter
    def parent(self, parent):
        self._parent = None if parent is None else weakref.ref(parent)

    @property
    def scope(self):
        """The scoping Component that is used to resolve the Component names in
        this Workflow.
        """
        if self._scope is None and self.parent is not None:
            self._scope = weakref.ref(self.parent.get_expr_scope())
        if self._scope is None:
            raise RuntimeError("workflow has no scope!")
        return self._scope()

    @scope.setter
    def scope(self, scope):
        self._scope = None if scope is None else weakref.ref(scope)
        self.config_changed()

    @property
    def itername(self):
        return self._iterbase()

    def check_config(self, strict=False):
        """Perform any checks that we need prior to run. Specific workflows
        should override this."""
        pass

    def set_initial_count(self, count):
        """
        Set initial value for execution count.  Only needed if the iteration
        coordinates must be initialized, such as for CaseIterDriverBase.

        count: int
            Initial value for workflow execution count.
        """
        self._initial_count = count - 1  # run() and step() will increment.

    def reset(self):
        """ Reset execution count. """
        self._exec_count = self._initial_count

    def run(self, ffd_order=0, case_uuid=None):
        """ Run the Components in this Workflow. """
        self._stop = False
        self._exec_count += 1

        iterbase = self._iterbase()

        if case_uuid is None:
            # We record the case and are responsible for unique case ids.
            record_case = True
            case_uuid = Case.next_uuid()
        else:
            record_case = False

        err = None
        #try:
        self._system.run(iterbase=iterbase, ffd_order=ffd_order,
                            case_uuid=case_uuid)

        if self._stop:
            raise RunStopped('Stop requested')
        #except Exception as exc:
        #    err = TracedError(exc, format_exc())

        if record_case and self._rec_required:
            try:
                self._record_case(case_uuid, err)
            except Exception as exc:
                if err is None:
                    err = TracedError(exc, format_exc())
                self.parent._logger.error("Can't record case: %s", exc)

        if err is not None:
            err.reraise()

    def calc_gradient(self, inputs=None, outputs=None, mode='auto',
                      return_format='array'):
        """Returns the Jacobian of derivatives between inputs and outputs.

        inputs: list of strings
            List of OpenMDAO inputs to take derivatives with respect to.

        outputs: list of strings
            Lis of OpenMDAO outputs to take derivatives of.

        mode: string in ['forward', 'adjoint', 'auto']
            Mode for gradient calculation. Set to 'auto' to let OpenMDAO choose
            forward or adjoint based on problem dimensions.

        return_format: string in ['array', 'dict']
            Format for return value. Default is array, but some optimizers may
            want a dictionary instead.
        """

        parent = self.parent
        reset = False

        # TODO - Support automatic determination of mode

        if self._system is None:
            reset = True
        else:
            uvec = self._system.vec['u']
            
        if self.scope._setup_inputs != inputs or self.scope._setup_outputs != outputs:
            reset = True

        if inputs is None:
            if hasattr(parent, 'list_param_group_targets'):
                inputs = parent.list_param_group_targets()
            if not inputs:
                msg = "No inputs given for derivatives."
                self.scope.raise_exception(msg, RuntimeError)
        elif reset is False:
            for inp in inputs:
                if inp not in uvec:
                    reset = True
                    break

        # If outputs aren't specified, use the objectives and constraints
        if outputs is None:
            outputs = []
            if hasattr(parent, 'list_objective_targets'):
                outputs.extend(parent.list_objective_targets())
            if hasattr(parent, 'list_constraint_targets'):
                outputs.extend(parent.list_constraint_targets())
        elif reset is False:
            for out in outputs:
                if out not in uvec:
                    reset = True
                    break

        if reset:
            # recreate system hierarchy
            self.scope._setup(inputs=inputs, outputs=outputs)

        return self._system.calc_gradient(inputs, outputs, mode=mode,
                                          options=self.parent.gradient_options,
                                          iterbase=self._iterbase(),
                                          return_format=return_format)

    def calc_newton_direction(self):
        """ Solves for the new state in Newton's method and leaves it in the
        df vector."""

        self._system.calc_newton_direction(options=self.parent.gradient_options,
                                          iterbase=self._iterbase())

    def check_gradient(self, inputs=None, outputs=None, stream=sys.stdout, mode='auto'):
        """Compare the OpenMDAO-calculated gradient with one calculated
        by straight finite-difference. This provides the user with a way
        to validate his derivative functions (apply_deriv and provideJ.)
        Note that fake finite difference is turned off so that we are
        doing a straight comparison.

        inputs: (optional) iter of str or None
            Names of input variables. The calculated gradient will be
            the matrix of values of the output variables with respect
            to these input variables. If no value is provided for inputs,
            they will be determined based on the parameters of
            the Driver corresponding to this workflow.

        outputs: (optional) iter of str or None
            Names of output variables. The calculated gradient will be
            the matrix of values of these output variables with respect
            to the input variables. If no value is provided for outputs,
            they will be determined based on the objectives and constraints
            of the Driver corresponding to this workflow.

        stream: (optional) file-like object or str
            Where to write to, default stdout. If a string is supplied,
            that is used as a filename. If None, no output is written.

        mode: (optional) str
            Set to 'forward' for forward mode, 'adjoint' for adjoint mode,
            or 'auto' to let OpenMDAO determine the correct mode.
            Defaults to 'auto'.

        Returns the finite difference gradient, the OpenMDAO-calculated
        gradient, and a list of suspect inputs/outputs.
        """
        parent = self.parent

        # tuples cause problems
        if inputs:
            inputs = list(inputs)
        if outputs:
            outputs = list(outputs)

        if isinstance(stream, basestring):
            stream = open(stream, 'w')
            close_stream = True
        else:
            close_stream = False
            if stream is None:
                stream = StringIO()

        J = self.calc_gradient(inputs, outputs, mode=mode)
        Jbase = self.calc_gradient(inputs, outputs, mode='fd')

        print >> stream, 24*'-'
        print >> stream, 'Calculated Gradient'
        print >> stream, 24*'-'
        print >> stream, J
        print >> stream, 24*'-'
        print >> stream, 'Finite Difference Comparison'
        print >> stream, 24*'-'
        print >> stream, Jbase

        # This code duplication is needed so that we print readable names for
        # the constraints and objectives.

        if inputs is None:
            if hasattr(parent, 'list_param_group_targets'):
                inputs = parent.list_param_group_targets()
                input_refs = []
                for item in inputs:
                    if len(item) < 2:
                        input_refs.append(item[0])
                    else:
                        input_refs.append(item)
            # Should be caught in calc_gradient()
            else:  # pragma no cover
                msg = "No inputs given for derivatives."
                self.scope.raise_exception(msg, RuntimeError)
        else:
            input_refs = inputs

        if outputs is None:
            outputs = []
            output_refs = []
            if hasattr(parent, 'get_objectives'):
                obj = ["%s.out0" % item.pcomp_name for item in
                       parent.get_objectives().values()]
                outputs.extend(obj)
                output_refs.extend(parent.get_objectives().keys())
            if hasattr(parent, 'get_constraints'):
                con = ["%s.out0" % item.pcomp_name for item in
                       parent.get_constraints().values()]
                outputs.extend(con)
                output_refs.extend(parent.get_constraints().keys())

            if len(outputs) == 0:  # pragma no cover
                msg = "No outputs given for derivatives."
                self.scope.raise_exception(msg, RuntimeError)
        else:
            output_refs = outputs

        out_width = 0

        for output, oref in zip(outputs, output_refs):
            out_val = self.scope.get(output)
            out_names = _flattened_names(oref, out_val)
            out_width = max(out_width, max([len(out) for out in out_names]))

        inp_width = 0
        for input_tup, iref in zip(inputs, input_refs):
            if isinstance(input_tup, str):
                input_tup = [input_tup]
            inp_val = self.scope.get(input_tup[0])
            inp_names = _flattened_names(str(iref), inp_val)
            inp_width = max(inp_width, max([len(inp) for inp in inp_names]))

        label_width = out_width + inp_width + 4

        print >> stream
        print >> stream, label_width*' ', \
              '%-18s %-18s %-18s' % ('Calculated', 'FiniteDiff', 'RelError')
        print >> stream, (label_width+(3*18)+3)*'-'

        suspect_limit = 1e-5
        error_n = error_sum = 0
        error_max = error_loc = None
        suspects = []
        i = -1

        io_pairs = []

        for output, oref in zip(outputs, output_refs):
            out_val = self.scope.get(output)
            for out_name in _flattened_names(oref, out_val):
                i += 1
                j = -1
                for input_tup, iref in zip(inputs, input_refs):
                    if isinstance(input_tup, basestring):
                        input_tup = (input_tup,)

                    inp_val = self.scope.get(input_tup[0])
                    for inp_name in _flattened_names(iref, inp_val):
                        j += 1
                        calc = J[i, j]
                        finite = Jbase[i, j]
                        if finite and calc:
                            error = (calc - finite) / finite
                        else:
                            error = calc - finite
                        error_n += 1
                        error_sum += abs(error)
                        if error_max is None or abs(error) > abs(error_max):
                            error_max = error
                            error_loc = (out_name, inp_name)
                        if abs(error) > suspect_limit or isnan(error):
                            suspects.append((out_name, inp_name))
                        print >> stream, '%*s / %*s: %-18s %-18s %-18s' \
                              % (out_width, out_name, inp_width, inp_name,
                                 calc, finite, error)
                        io_pairs.append("%*s / %*s"
                                        % (out_width, out_name,
                                           inp_width, inp_name))
        print >> stream
        if error_n:
            print >> stream, 'Average RelError:', error_sum / error_n
            print >> stream, 'Max RelError:', error_max, 'for %s / %s' % error_loc
        if suspects:
            print >> stream, 'Suspect gradients (RelError > %s):' % suspect_limit
            for out_name, inp_name in suspects:
                print >> stream, '%*s / %*s' \
                      % (out_width, out_name, inp_width, inp_name)
        print >> stream

        if close_stream:
            stream.close()

        # return arrays and suspects to make it easier to check from a test
        return Jbase.flatten(), J.flatten(), io_pairs, suspects

    def configure_recording(self, includes, excludes):
        """Called at start of top-level run to configure case recording.
        Returns set of paths for changing inputs."""
        if not includes:
            self._rec_required = False
            return (set(), dict())

        driver = self.parent
        scope = driver.parent
        prefix = scope.get_pathname()
        if prefix:
            prefix += '.'
        inputs = []
        outputs = []

        # Parameters
        self._rec_parameters = []
        if hasattr(driver, 'get_parameters'):
            for name, param in driver.get_parameters().items():
                if isinstance(name, tuple):
                    name = name[0]
                path = prefix+name
                if self._check_path(path, includes, excludes):
                    self._rec_parameters.append(param)
                    inputs.append(name)

        # Objectives
        self._rec_objectives = []
        if hasattr(driver, 'eval_objectives'):
            for key, objective in driver.get_objectives().items():
                name = objective.pcomp_name
                path = prefix+name
                if self._check_path(path, includes, excludes):
                    self._rec_objectives.append(key)
                    outputs.append(name)

        # Responses
        self._rec_responses = []
        if hasattr(driver, 'get_responses'):
            for key, response in driver.get_responses().items():
                name = response.pcomp_name
                path = prefix+name
                if self._check_path(path, includes, excludes):
                    self._rec_responses.append(key)
                    outputs.append(name)

        # Constraints
        self._rec_constraints = []
        if hasattr(driver, 'get_eq_constraints'):
            for con in driver.get_eq_constraints().values():
                name = con.pcomp_name
                path = prefix+name
                if self._check_path(path, includes, excludes):
                    self._rec_constraints.append(con)
                    outputs.append(name)
        if hasattr(driver, 'get_ineq_constraints'):
            for con in driver.get_ineq_constraints().values():
                name = con.pcomp_name
                path = prefix+name
                if self._check_path(path, includes, excludes):
                    self._rec_constraints.append(con)
                    outputs.append(name)

        # Other outputs.
        self._rec_outputs = []
        srcs = scope.list_inputs()
        if hasattr(driver, 'get_parameters'):
            srcs.extend(param.target
                        for param in driver.get_parameters().values())
        dsts = scope.list_outputs()
        if hasattr(driver, 'get_objectives'):
            dsts.extend(objective.pcomp_name+'.out0'
                        for objective in driver.get_objectives().values())
        if hasattr(driver, 'get_responses'):
            dsts.extend(response.pcomp_name+'.out0'
                        for response in driver.get_responses().values())
        if hasattr(driver, 'get_eq_constraints'):
            dsts.extend(constraint.pcomp_name+'.out0'
                        for constraint in driver.get_eq_constraints().values())
        if hasattr(driver, 'get_ineq_constraints'):
            dsts.extend(constraint.pcomp_name+'.out0'
                        for constraint in driver.get_ineq_constraints().values())

        graph = scope._depgraph
        for src, dst in _get_inner_connections(graph, srcs, dsts):
            if scope.get_metadata(src)['iotype'] == 'in':
                continue
            path = prefix+src
            if src not in inputs and src not in outputs and \
               self._check_path(path, includes, excludes):
                self._rec_outputs.append(src)
                outputs.append(src)

        for comp in self.get_components():
            for name in comp.list_outputs():
                src = '%s.%s' % (comp.name, name)
                path = prefix+src
                if src not in outputs and \
                   self._check_path(path, includes, excludes):
                    self._rec_outputs.append(src)
                    outputs.append(src)

        name = '%s.workflow.itername' % driver.name
        path = prefix+name
        if self._check_path(path, includes, excludes):
            self._rec_outputs.append(name)
            outputs.append(name)

        # If recording required, register names in recorders.
        self._rec_required = bool(inputs or outputs)
        if self._rec_required:
            top = scope
            while top.parent is not None:
                top = top.parent
            for recorder in top.recorders:
                recorder.register(driver, inputs, outputs)

        return (set(prefix+name for name in inputs), dict())

    @staticmethod
    def _check_path(path, includes, excludes):
        """ Return True if `path` should be recorded. """
        for pattern in includes:
            if fnmatch(path, pattern):
                break
        else:
            return False

        for pattern in excludes:
            if fnmatch(path, pattern):
                return False
        return True

    def _record_case(self, case_uuid, err):
        """ Record case in all recorders. """
        driver = self.parent
        scope = driver.parent
        top = scope
        while top.parent is not None:
            top = top.parent

        inputs = []
        outputs = []

        # Parameters.
        for param in self._rec_parameters:
            try:
                value = param.evaluate(scope)
            except Exception as exc:
                driver.raise_exception("Can't evaluate '%s' for recording: %s"
                                       % (param, exc), RuntimeError)

            if param.size == 1:  # evaluate() always returns list.
                value = value[0]
            inputs.append(value)

        # Objectives.
        for key in self._rec_objectives:
            try:
                outputs.append(driver.eval_named_objective(key))
            except Exception as exc:
                driver.raise_exception("Can't evaluate '%s' for recording: %s"
                                       % (key, exc), RuntimeError)
        # Responses.
        for key in self._rec_responses:
            try:
                outputs.append(driver.eval_response(key))
            except Exception as exc:
                driver.raise_exception("Can't evaluate '%s' for recording: %s"
                                       % (key, exc), RuntimeError)
        # Constraints.
        for con in self._rec_constraints:
            try:
                value = con.evaluate(scope)
            except Exception as exc:
                driver.raise_exception("Can't evaluate '%s' for recording: %s"
                                       % (con, exc), RuntimeError)
            if len(value) == 1:  # evaluate() always returns list.
                value = value[0]
            outputs.append(value)

        # Other outputs.
        for name in self._rec_outputs:
            try:
                outputs.append(scope.get(name))
            except Exception as exc:
                scope.raise_exception("Can't get '%s' for recording: %s"
                                      % (name, exc), RuntimeError)
        # Record.
        for recorder in top.recorders:
            recorder.record(driver, inputs, outputs, err,
                            case_uuid, self.parent._case_uuid)

    def _iterbase(self):
        """ Return base for 'iteration coordinates'. """
        if self.parent is None:
            return str(self._exec_count)  # An unusual case.
        else:
            prefix = self.parent.get_itername()
            if prefix:
                prefix += '.'
            return '%s%d' % (prefix, self._exec_count)

    def stop(self):
        """
        Stop all Components in this Workflow.
        We assume it's OK to to call stop() on something that isn't running.
        """
        self._system.stop()
        self._stop = True

    def add(self, compnames, index=None, check=False):
        """ Add new component(s) to the workflow by name."""
        raise NotImplementedError("This Workflow has no 'add' function")

    def config_changed(self):
        """Notifies the Workflow that workflow configuration
        (dependencies, etc.) has changed.
        """
        self._wf_comp_graph = None
        self._var_graph = None
        self._system = None

    def remove(self, comp):
        """Remove a component from this Workflow by name."""
        raise NotImplementedError("This Workflow has no 'remove' function")

    def get_names(self, full=False):
        """Return a list of component names in this workflow."""
        raise NotImplementedError("This Workflow has no 'get_names' function")

    def get_components(self, full=False):
        """Returns a list of all component objects in the workflow. No ordering
        is assumed.
        """
        scope = self.scope
        return [getattr(scope, name) for name in self.get_names(full)]

    def __iter__(self):
        """Returns an iterator over the components in the workflow in
        some order.
        """
        raise NotImplementedError("This Workflow has no '__iter__' function")

    def __len__(self):
        raise NotImplementedError("This Workflow has no '__len__' function")

    ## MPI stuff ##

    def setup_systems(self, system_type):
        """Get the subsystem for this workflow. Each
        subsystem contains a subgraph of this workflow's component
        graph, which contains components and/or other subsystems.
        Returns the names of any added systems.
        """

        scope = self.scope
        name2collapsed = scope.name2collapsed
        reduced = self.parent.get_reduced_graph()
        reduced = reduced.subgraph(reduced.nodes_iter())
        added = set()

        # remove our driver from the reduced graph
        if self.parent.name in reduced:
            reduced.remove_node(self.parent.name)

        params = []
        if hasattr(self.parent, 'list_param_targets'):
            params = self.parent.list_param_targets()

        # we need to connect a param comp node to all param nodes
        for param in params:
            reduced.add_node(param, comp='param')
            reduced.add_edge(param, name2collapsed[param])
            added.add(param)
            reduced.node[param]['system'] = \
                       ParamSystem(scope, reduced, param)

        if self.scope._setup_inputs is not None:
            for param in simple_node_iter(self.scope._setup_inputs):
                if param not in reduced:
                    reduced.add_node(param, comp='param')
                    reduced.add_edge(param, name2collapsed[param])
                    reduced.node[param]['system'] = \
                               ParamSystem(scope, reduced, param)
                added.add(param)

        cgraph = reduced2component(reduced)

        # collapse driver iteration sets into a single node for
        # the driver, except for nodes from their iteration set
        # that are in the iteration set of their parent driver.
        self.parent._collapse_subdrivers(cgraph)

        if MPI and system_type == 'auto':
            self._auto_setup_systems(scope, reduced, cgraph)
        elif MPI and system_type == 'parallel':
            self._system = ParallelSystem(scope, reduced, cgraph, 
                                          str(tuple(cgraph.nodes())))
        else:
            self._system = SerialSystem(scope, reduced, cgraph, 
                                        str(tuple(cgraph.nodes())))

        self._system.set_ordering(params+[c.name for c in self])

        self._system._parent_system = self.scope._reduced_graph.node[self.parent.name]['system']

        for comp in self:
            added.update(comp.setup_systems())

        return added

    def _auto_setup_systems(self, scope, reduced, cgraph):
        """
        Collapse the graph (recursively) into nodes representing
        subsystems.
        """
        cgraph = partition_subsystems(scope, reduced, cgraph)

        if len(cgraph) > 1:
            if len(cgraph.edges()) > 0:
                self._system = SerialSystem(scope, reduced, cgraph, tuple(cgraph.nodes()))
            else:
                self._system = ParallelSystem(scope, reduced, cgraph, str(tuple(cgraph.nodes())))
        elif len(cgraph) == 1:
            name = cgraph.nodes()[0]
            self._system = cgraph.node[name].get('system')
        else:
            raise RuntimeError("setup_systems called on %s.workflow but component graph is empty!" %
                                self.parent.get_pathname())

    def get_req_cpus(self):
        """Return requested_cpus"""
        if self._system is None:
            return 1
        else:
            return self._system.get_req_cpus()

    def setup_communicators(self, comm):
        """Allocate communicators from here down to all of our
        child Components.
        """
        self.mpi.comm = get_comm_if_active(self, comm)
        if MPI and self.mpi.comm == MPI.COMM_NULL:
            return
        #mpiprint("workflow for %s: setup_comms" % self.parent.name)
        self._system.setup_communicators(self.mpi.comm)

    def setup_variables(self):
        if MPI and self.mpi.comm == MPI.COMM_NULL:
            return
        return self._system.setup_variables()

    def setup_sizes(self):
        if MPI and self.mpi.comm == MPI.COMM_NULL:
            return
        return self._system.setup_sizes()

    def setup_vectors(self, arrays=None, state_resid_map=None):
        if MPI and self.mpi.comm == MPI.COMM_NULL:
            return
        self._system.setup_vectors(arrays, state_resid_map)

    def setup_scatters(self):
        if MPI and self.mpi.comm == MPI.COMM_NULL:
            return
        self._system.setup_scatters()

    def get_full_nodeset(self):
        """Return the set of nodes in the depgraph
        belonging to this driver (inlcudes full iteration set).
        """
        nodeset = set([c.name for c in self.parent.iteration_set()])

        rgraph = self.scope._reduced_graph

        # some drivers don't have parameters but we still may want derivatives
        # w.r.t. other inputs.  Look for param comps that attach to comps in
        # our nodeset
        for node, data in rgraph.nodes_iter(data=True):
            if data.get('comp') == 'param':
                if node.split('.', 1)[0] in nodeset:
                    nodeset.add(node)

        return nodeset
