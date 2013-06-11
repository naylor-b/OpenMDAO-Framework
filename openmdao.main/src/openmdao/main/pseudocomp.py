
import threading

from openmdao.main.expreval import ExprEvaluator
from openmdao.main.printexpr import transform_expression


class PseudoComponent(object):
    """A 'fake' component that is constructed from an ExprEvaluator.
    This fake component can be added to a dependency graph and executed
    along with 'real' components.
    """

    _lock = threading.RLock()
    _count = 0

    def __init__(self, expr, scope):
        with self._lock:
            self.name = '#%d' % self._count
            self._count += 1

        self._mapping = {}

        if isinstance(expr, basestring):
            expr = ExprEvaluator(expr, scope=scope)
        else:
            expr.scope = scope

        self._inputrefs = expr.rhsrefs()
        self._outputref = expr.lhsref()

        for i,ref in enumerate(self._inputrefs):
            in_name = 'in%d' % i
            self._mapins[ref] = in_name
            setattr(self, in_name, None)

        if self._outputref: 
            setattr(self, 'out1', None)

        xformed = transform_expression(expr.text, self._mapping)

        self.expr = ExprEvaluator(xformed)

    def make_connections(self, depgraph):
        """Connect all of the inputs and outputs of this comp to
        the appropriate nodes in the dependency graph.
        """
        
        for ref in self._inputrefs:
            invar = self._mapping[ref]

    def invalidate_deps(varnames, force=False):
        return None

    def run(self):
        self.expr.evaluate(self)



