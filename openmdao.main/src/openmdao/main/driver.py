""" Driver class definition """

#public symbols
__all__ = ["Driver"]

import fnmatch

from networkx.algorithms.shortest_paths.generic import shortest_path
from enthought.traits.api import List

# pylint: disable-msg=E0611,F0401

from openmdao.main.interfaces import ICaseRecorder, IDriver, IComponent, ICaseIterator, \
                                     IHasEvents, implements
from openmdao.main.exceptions import RunStopped
from openmdao.main.expreval import ExprEvaluator
from openmdao.main.component import Component
from openmdao.main.workflow import Workflow
from openmdao.main.case import Case
from openmdao.main.dataflow import Dataflow
from openmdao.main.hasevents import HasEvents
from openmdao.main.hasparameters import HasParameters
from openmdao.main.hasconstraints import HasConstraints, HasEqConstraints, HasIneqConstraints
from openmdao.main.hasobjective import HasObjective, HasObjectives
from openmdao.main.hasevents import HasEvents
from openmdao.util.decorators import add_delegate
from openmdao.main.mp_support import is_instance, has_interface
from openmdao.main.rbac import rbac
from openmdao.main.datatypes.api import Slot, Str

@add_delegate(HasEvents)
class Driver(Component):
    """ A Driver iterates over a workflow of Components until some condition
    is met. """
    
    implements(IDriver, IHasEvents)

    recorders = List(Slot(ICaseRecorder, required=False), 
                     desc='Case recorders for iteration data.')
    
    # Extra variables for printing
    printvars = List(Str, iotype='in', desc='List of extra variables to '
                               'output in the recorders.')
    

    # set factory here so we see a default value in the docs, even
    # though we replace it with a new Dataflow in __init__
    workflow = Slot(Workflow, allow_none=True, required=True, factory=Dataflow)
    
    def __init__(self, doc=None):
        self._iter = None
        super(Driver, self).__init__(doc=doc)
        self.workflow = Dataflow(self)
        self.force_execute = True 
 
    def _workflow_changed(self, oldwf, newwf):
        if newwf is not None:
            newwf._parent = self

    def get_expr_scope(self):
        """Return the scope to be used to evaluate ExprEvaluators."""
        return self.parent

    def is_valid(self):
        """Return False if any Component in our workflow(s) is invalid,
        or if any of our variables is invalid.
        """
        if super(Driver, self).is_valid() is False:
            return False

        # force execution if any component in the workflow is invalid
        for comp in self.workflow.get_components():
            if not comp.is_valid():
                return False
        return True

    def check_config (self):
        """Verify that our workflow is able to resolve all of its components."""
        # workflow will raise an exception if it can't resolve a Component
        super(Driver, self).check_config()
        # if workflow is not defined, or if it contains only Drivers, try to
        # use parameters, objectives and/or constraint expressions to
        # determine the necessary workflow members
        try:
            iterset = set(c.name for c in self.iteration_set())
            alldrivers = all([isinstance(c, Driver) 
                                for c in self.workflow.get_components()])
            reqcomps = self._get_required_compnames()
            if len(self.workflow) == 0:
                self.workflow.add(reqcomps)
            elif alldrivers is True:
                self.workflow.add([name for name in reqcomps 
                                        if name not in iterset])
            else:
                diff = reqcomps - iterset
                if len(diff) > 0:
                    #raise RuntimeError("Expressions in this Driver require the following "
                    #                   "Components that are not part of the "
                    #                   "workflow: %s" % list(diff))
                    pass
            # calling get_components() here just makes sure that all of the
            # components can be resolved
            comps = self.workflow.get_components()
        except Exception as err:
            self.raise_exception(str(err), type(err))

    def iteration_set(self):
        """Return a set of all Components in our workflow(s), and 
        recursively in any workflow in any Driver in our workflow(s).
        """
        allcomps = set()
        if len(self.workflow) == 0:
            for compname in self._get_required_compnames():
                self.workflow.add(compname)
        for child in self.workflow.get_components():
            allcomps.add(child)
            if has_interface(child, IDriver):
                allcomps.update(child.iteration_set())
        return allcomps
        
    @rbac(('owner', 'user'))
    def get_expr_depends(self):
        """Returns a list of tuples of the form (src_comp_name,
        dest_comp_name) for each dependency introduced by any ExprEvaluators
        in this Driver, ignoring any dependencies on components that are
        inside of this Driver's iteration set.
        """
        iternames = set([c.name for c in self.iteration_set()])
        conn_list = super(Driver, self).get_expr_depends()
        new_list = []
        for src, dest in conn_list:
            if src not in iternames and dest not in iternames:
                new_list.append((src, dest))
        return new_list

    def _get_required_compnames(self):
        """Returns a set of names of components that are required by 
        this Driver in order to evaluate parameters, objectives
        and constraints.  This list will include any intermediate
        components in the data flow between components referenced by
        parameters and those referenced by objectives and/or constraints.
        """
        setcomps = set()
        getcomps = set()

        if hasattr(self, '_delegates_'):
            for name, dclass in self._delegates_.items():
                inst = getattr(self, name)
                if isinstance(inst, HasParameters):
                    setcomps = inst.get_referenced_compnames()
                elif isinstance(inst, (HasConstraints, HasEqConstraints, 
                                       HasIneqConstraints, HasObjective, HasObjectives)):
                    getcomps.update(inst.get_referenced_compnames())

        full = set(getcomps)
        full.update(setcomps)
        
        if self.parent:
            graph = self.parent._depgraph
            for end in getcomps:
                for start in setcomps:
                    full.update(graph.find_all_connecting(start, end))
        return full

    @rbac('*', 'owner')
    def run (self, force=False, ffd_order=0, case_id=''):
        """Run this object. This should include fetching input variables if necessary,
        executing, and updating output variables. Do not override this function.

        force: bool
            If True, force component to execute even if inputs have not
            changed. (Default is False)
            
        ffd_order: int
            Order of the derivatives to be used when finite differncing (1 for first
            derivatives, 2 for second derivativse). During regular execution,
            ffd_order should be 0. (Default is 0)
            
        case_id: str
            Identifier for the Case that is associated with this run. (Default is '')
            If applied to the top-level assembly, this will be prepended to
            all iteration coordinates.
        """
        # Override just to reset the workflow :-(
        self.workflow.reset()
        super(Driver, self).run(force, ffd_order, case_id)

    def execute(self):
        """ Iterate over a workflow of Components until some condition
        is met. If you don't want to structure your driver to use *pre_iteration*,
        *post_iteration*, etc., just override this function. As a result, none
        of the <start/pre/post/continue>_iteration() functions will be called.
        """
        self._iter = None
        self.start_iteration()
        while self.continue_iteration():
            self.pre_iteration()
            self.run_iteration()
            self.post_iteration()

    def step(self):
        """Similar to the 'execute' function, but this one only 
        executes a single Component from the workflow each time
        it's called.
        """
        if self._iter is None:
            self.start_iteration()
            self._iter = self._step()
        try:
            self._iter.next()
        except StopIteration:
            self._iter = None
            raise
        raise RunStopped('Step complete')
        
    def _step(self):
        while self.continue_iteration():
            self.pre_iteration()
            for junk in self._step_workflow():
                yield
            self.post_iteration()
        self._iter = None
        raise StopIteration()
    
    def _step_workflow(self):
        while True:
            try:
                self.workflow.step()
            except RunStopped:
                pass
            yield

    def stop(self):
        self._stop = True
        self.workflow.stop()

    def start_iteration(self):
        """Called just prior to the beginning of an iteration loop. This can 
        be overridden by inherited classes. It can be used to perform any 
        necessary pre-iteration initialization.
        """
        self._continue = True

    def continue_iteration(self):
        """Return False to stop iterating."""
        return self._continue
    
    def pre_iteration(self):
        """Called prior to each iteration.  This is where iteration events are set."""
        self.set_events()
        
        
    def run_iteration(self):
        """Runs workflow."""
        wf = self.workflow
        if len(wf) == 0:
            self._logger.warning("'%s': workflow is empty!" % self.get_pathname())
        wf.run(ffd_order=self.ffd_order, case_id=self._case_id)
        
    def calc_derivatives(self, first=False, second=False):
        """ Calculate derivatives and save baseline states for all components
        in this workflow."""
        self.workflow.calc_derivatives(first, second)
        
    def check_derivatives(self, order, driver_inputs, driver_outputs):
        """ Check derivatives for all components in this workflow."""
        self.workflow.check_derivatives(order, driver_inputs, driver_outputs)
        
    def post_iteration(self):
        """Called after each iteration."""
        self._continue = False  # by default, stop after one iteration

    def config_changed(self, update_parent=True):
        """Call this whenever the configuration of this Component changes,
        for example, children are added or removed or dependencies may have
        changed.
        """
        super(Driver, self).config_changed(update_parent)
        if self.workflow is not None:
            self.workflow.config_changed()
            
    def record_case(self):
        """ A driver can call this function to record the current state of the
        current iteration as a Case into all slotted case recorders. Generally,
        the driver should call this function once per iteration, and may also
        need to call it at the conclusion.
        
        All paramters, objectives, and constraints are included in the Case
        output, along with all extra variables listed in self.printvars.
        """

        if not self.recorders:
            return
        
        case_input = []
        case_output = []
        
        # Parameters
        if hasattr(self, 'get_parameters'):
            for name, param in self.get_parameters().iteritems():
                if isinstance(name, tuple):
                    name = name[0]
                case_input.append([name, param.evaluate(self.parent)])
          
        # Objectives
        if hasattr(self, 'eval_objective'):
            case_output.append(["Objective", self.eval_objective()])
    
        # Constraints
        if hasattr(self, 'get_ineq_constraints'):
            for name, con in self.get_ineq_constraints().iteritems():
                val = con.evaluate(self.parent)
                if '>' in val[2]:
                    case_output.append(["Constraint ( %s )" % name,
                                                              val[0]-val[1]])
                else:
                    case_output.append(["Constraint ( %s )" % name,
                                                              val[1]-val[0]])
            
        if hasattr(self, 'get_eq_constraints'):
            for name, con in self.get_eq_constraints().iteritems():
                val = con.evaluate(self.parent)
                case_output.append(["Constraint ( %s )" % name, val[1]-val[0]])
            
        # Additional user-requested variables
        for printvar in self.printvars:
            
            if  '*' in printvar:
                printvars = self._get_all_varpaths(printvar)
            else:
                printvars = [printvar]
                
            for var in printvars:
                iotype = self.parent.get_metadata(var, 'iotype')
                if iotype == 'in':
                    val = ExprEvaluator(var, scope=self.parent).evaluate()
                    case_input.append([var, val])
                elif iotype == 'out':
                    val = ExprEvaluator(var, scope=self.parent).evaluate()
                    case_output.append([var, val])
                else:
                    msg = "%s is not an input or output" % var
                    self.raise_exception(msg, ValueError)
                        
        # Pull iteration coord from workflow
        coord = self.workflow._iterbase('')
        
        case = Case(case_input, case_output, label=coord,
                    parent_uuid=self._case_id)
        
        for recorder in self.recorders:
            recorder.record(case)
        
    def _get_all_varpaths(self, pattern, header=''):
        ''' Return a list of all varpaths in the driver's workflow that
        match the specified pattern.
        
        Used by record_case.'''
        
        # assume we don't want this in driver's imports
        from openmdao.main.assembly import Assembly

        # Start with our driver's settings
        all_vars = []
        for var in self.list_vars():
            all_vars.append('%s.%s' % (self.name, var))
        
        
        for comp in self.workflow.__iter__():
            
            # All variables from components in workflow
            for var in comp.list_vars():
                all_vars.append('%s%s.%s' % (header, comp.name, var))

            # Recurse into assemblys
            if isinstance(comp, Assembly):
                
                assy_header = '%s%s.' % (header, comp.name)
                assy_vars = comp.driver._get_all_varpaths(pattern, assy_header)
                all_vars = all_vars + assy_vars
                
                
        # Match pattern in our var names
        matched_vars = []
        if pattern == '*':
            matched_vars = all_vars
        else:
            matched_vars = fnmatch.filter(all_vars, pattern)
        
        return matched_vars
                
            
            