
from openmdao.main.expreval import ExprEvaluator
from openmdao.main.printexpr import transform_expression

class PseudoComponent(object):
    """A 'fake' component that is constructed from an ExprEvaluator.
    This fake component can be added to a dependency graph and executed
    along with 'real' components.
    """

    def __init__(self, expr, scope):
        self._mapping = {}

        if isinstance(expr, basestring):
            expr = ExprEvaluator(expr, scope=scope)
        else:
            expr.scope = scope

        self._rhsrefs = expr.rhsrefs()
        self._lhsref = expr.lhsref()

        for i,ref in enumerate(self._rhsrefs):
            in_name = 'in%d' % i
            self._mapins[ref] = in_name
            setattr(self, in_name, None)

        if self._lhsref: 
            setattr(self, 'out1', None)

        xformed = transform_expression(expr.text, self._mapping)

        self.expr = ExprEvaluator(xformed)

    def make_connections(self, depgraph):
        """Connect all of the inputs and outputs of this comp to
        the appropriate nodes in the dependency graph.
        """
        
        pass

    def run(self):
        self.expr.evaluate(self)



