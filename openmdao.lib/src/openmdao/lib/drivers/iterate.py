"""
This is a simple iteration driver that basically runs a workflow, passing the
output to the input for the next iteration. Relative change and number of
iterations are used as termination criteria.
"""

from openmdao.main.mpiwrap import MPI

if not MPI:
    from numpy.linalg import norm

from openmdao.main.datatypes.api import Float, Int, Bool, Enum
from openmdao.main.api import Driver, CyclicWorkflow
from openmdao.util.decorators import add_delegate
from openmdao.main.hasstopcond import HasStopConditions
from openmdao.main.hasparameters import HasParameters
from openmdao.main.hasconstraints import HasEqConstraints
from openmdao.main.interfaces import IHasParameters, IHasEqConstraints, \
                                     ISolver, implements

@add_delegate(HasParameters, HasEqConstraints, HasStopConditions)
class FixedPointIterator(Driver):
    """ A simple fixed point iteration driver, which runs a workflow and passes
    the value from the output to the input for the next iteration. Relative
    change and number of iterations are used as termination criteria. This type
    of iteration is also known as Gauss-Seidel."""

    implements(IHasParameters, IHasEqConstraints, ISolver)

    # pylint: disable-msg=E1101
    max_iteration = Int(25, iotype='in', desc='Maximum number of '
                                         'iterations before termination.')

    tolerance = Float(1.0e-3, iotype='in', desc='Absolute convergence '
                                            'tolerance between iterations.')

    norm_order = Enum('Infinity', ['Infinity', 'Euclidean'],
                       desc='For multivariable iteration, type of norm '
                                   'to use to test convergence.')

    def __init__(self):
        super(FixedPointIterator, self).__init__()
        self.current_iteration = 0
        self.workflow = CyclicWorkflow()
        self.normval = 1.e99
        self.norm0 = 1.e99

        # user either the petsc norm or numpy.linalg norm
        if MPI:
            self.norm = self._mpi_norm

    def execute(self):
        """ Executes an iterative solver """
        self.current_iteration = 0
        if MPI:
            if self.workflow._system.mpi.comm == MPI.COMM_NULL:
                return
        else:
            if self.norm_order == 'Infinity':
                self._norm_order = float('inf')
            else:
                self._norm_order = self.norm_order

        super(FixedPointIterator, self).execute()

    def start_iteration(self):
        """ Commands run before any iterations """
        self.current_iteration = 0
        self.normval = 1.e99
        self.norm0 = 1.e99
        self.run_iteration()
        self.normval = self.norm()
        self.norm0 = self.normval if self.normval != 0.0 else 1.0

    def run_iteration(self):
        self.current_iteration += 1
        system = self.workflow._system
        system.vec['u'].array += system.vec['f'].array[:]
        system.run(self.workflow._iterbase())

    def continue_iteration(self):
        return not self.should_stop() and \
               self.current_iteration < self.max_iteration and \
               self.normval > self.tolerance 
              # and self.normval/self.norm0 > self.rtol:

    def post_iteration(self):
        """Runs after each iteration"""
        self.normval = self.norm()
        #mpiprint("iter %d, norm = %s" % (self.current_iteration, self.normval))

    def _mpi_norm(self):
        """ Compute the norm of the f Vec using petsc. """
        fvec = self.workflow._system.vec['f']
        fvec.petsc_vec.assemble()
        return fvec.petsc_vec.norm()

    def norm(self):
        """ Compute the norm using numpy.linalg. """
        return norm(self.workflow._system.vec['f'].array, 
                    self._norm_order)

    def check_config(self, strict=False):
        """Make sure the problem is set up right."""

        super(FixedPointIterator, self).check_config(strict=strict)

        # We need to figure our severed edges before querying.
        eqcons = self.get_constraints().values()
        n_dep = len(eqcons)
        n_indep = len(self.get_parameters())

        if n_dep != n_indep:
            msg = "The number of input parameters must equal the number of" \
                  " output constraint equations in FixedPointIterator."
            self.raise_exception(msg, RuntimeError)

        # Check to make sure we don't have a null problem.
        if n_dep == 0:
            self.workflow._get_topsort()
            if len(self.workflow._severed_edges) == 0:
                msg = "FixedPointIterator requires a cyclic workflow, or a " \
                      "parameter/constraint pair."
                self.raise_exception(msg, RuntimeError)

        # Check the eq constraints to make sure they look ok.
        for eqcon in eqcons:

            if eqcon.rhs.text == '0' or eqcon.lhs.text == '0':
                msg = "Please specify constraints in the form 'A=B'"
                msg += ': %s = %s' % (eqcon.lhs.text, eqcon.rhs.text)
                self.raise_exception(msg, RuntimeError)

            if len(eqcon.get_referenced_varpaths()) > 2:
                msg = "Please specify constraints in the form 'A=B'"
                msg += ': %s = %s' % (eqcon.lhs.text, eqcon.rhs.text)
                self.raise_exception(msg, RuntimeError)


@add_delegate(HasStopConditions)
class IterateUntil(Driver):
    """ A simple driver to run a workflow until some stop condition is met. """

    max_iterations = Int(10, iotype="in", desc="Maximum number of iterations.")
    iteration = Int(0, iotype="out", desc="Current iteration counter.")
    run_at_least_once = Bool(True, iotype="in", desc="If True, driver will"
                             " ignore stop conditions for the first iteration"
                             " and run at least one iteration.")

    def start_iteration(self):
        """ Code executed before the iteration. """
        self.iteration = 0

    def continue_iteration(self):
        if self.iteration < 1 and self.run_at_least_once:
            self.iteration += 1
            return True

        if self.should_stop():
            return False

        if self.iteration < self.max_iterations:
            self.iteration += 1
            return True

        return False

