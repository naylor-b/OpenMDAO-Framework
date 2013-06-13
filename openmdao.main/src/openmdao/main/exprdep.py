
from openmdao.main.depgraph import DependencyGraph
from openmdao.main.expreval import ExprEvaluator, ConnectedExprEvaluator


class ExprDepGraph(object):
    """A dependency graph that can handle connections between expressions."""

    def __init__(self):
        self.exprs = {}  # dict of expr strings to ExprEvalutors
        self.dests = {}  # map of src expr string to list of dest expr strings
        self.srcs = {}   # map of dest expr string to src expr string

        self.depgraph = DependencyGraph()

    def __getattr__(self, name):  # for delegating stuff to depgraph
        return getattr(object.__getattribute__(self, 'depgraph'), name)

    def __contains__(self, name):
        return name in self.depgraph

    def vars_to_disconnect(self, scope, path, path2=None):
        """Returns a list of tuples of the form (srcvar, destvar) containing
        all variables that must be disconnected if path and path2 are
        disconnected.  If only path is given, then it's the name of a variable
        or a component.  If both path and path2 are given, then both
        must refer to variables.
        """
        if path2 is not None:
            return [(path, path2)]

        if scope.parent and '.' not in path:  # boundary var. make sure it's disconnected in parent
            scope.parent.disconnect('.'.join([scope.name, path]))
        # figure out if path is a variable or a component

        return self.depgraph.connections_to(path)

    def get_output_exprs(self):
        """Return all destination expressions at the output boundary"""
        return [e for txt,e in self.exprs.items() if txt in self.srcs and not e.get_referenced_compnames()]

    def get_expr(self, text):
        return self.exprs.get(text)

    def list_connections(self, show_passthrough=True):
        """Return a list of tuples of the form (outexpr, inexpr).
        """
        conn = []
        excludes = set([name for name, expr in self.exprs.items() if expr.refs_parent()])
        for src, dlist in self.dests.items():
            if src in excludes or not (show_passthrough or '.' in src):
                continue
            for dest in dlist:
                if dest in excludes:
                    continue
                if show_passthrough or '.' in dest:
                    conn.append((src, dest))
        return conn

    def get_source(self, dest_expr):
        """Returns the text of the source expression that is connected to the given
        destination expression.
        """
        return self.srcs.get(dest_expr)

    def get_dests(self, src_expr):
        """Returns the list of destination expressions (strings) that are connected to the given
        source expression.
        """
        return self.dests.get(src_expr, [])

    def remove(self, name):
        """Remove any connections referring to the given component or variable"""

        to_remove = []
        for expr in self.find_referring_exprs(name):
            to_remove.extend(self._remove_expr(expr))
        return to_remove

    def _remove_expr(self, txt):
        conns = set()
        del self.exprs[txt]

        if txt in self.srcs:
            conns.add((self.srcs[txt], txt))
            del self.srcs[txt]

        srcs_to_remove = []
        for src, dests in self.dests.items():
            try:
                dests.remove(txt)
            except ValueError:
                pass
            else: # found one
                conns.add((src, txt))
            if len(dests) == 0:
                srcs_to_remove.append(src)

        for rem in srcs_to_remove:
            del self.dests[rem]
            
        if txt in self.dests:
            for d in self.dests[txt]:
                conns.add((txt, d))
                del self.srcs[d]
            del self.dests[txt]
            
        return list(conns)

    def connect(self, src, dest, scope):
        if self.get_source(dest) is not None:
            scope.raise_exception("'%s' is already connected to source '%s'" % (dest, self.get_source(dest)),
                                  RuntimeError)

        destexpr = ConnectedExprEvaluator(dest, scope, getter='get_wrapped_attr',
                                          is_dest=True)
        srcexpr = ConnectedExprEvaluator(src, scope, getter='get_wrapped_attr')

        srccomps = srcexpr.get_referenced_compnames()
        destcomps = destexpr.get_referenced_compnames()

        if destcomps and destcomps.pop() in srccomps:
            raise RuntimeError("'%s' and '%s' refer to the same component." % (src, dest))

        srcvars = srcexpr.get_referenced_varpaths(copy=False)
        destvar = destexpr.get_referenced_varpaths().pop()

        destcompname, destcomp, destvarname = scope._split_varpath(destvar)
        desttrait = None

        if not destvar.startswith('parent.'):
            for srcvar in srcvars:
                if not srcvar.startswith('parent.'):
                    srccompname, srccomp, srcvarname = scope._split_varpath(srcvar)
                    src_io = 'in' if srccomp is scope else 'out'
                    srctrait = srccomp.get_dyn_trait(srcvarname, src_io)
                    if desttrait is None:
                        dest_io = 'out' if destcomp is scope else 'in'
                        desttrait = destcomp.get_dyn_trait(destvarname, dest_io)

            if not srcexpr.refs_parent() and desttrait is not None:
                # punt if dest is not just a simple var name.
                # validity will still be checked at execution time
                if destvar == destexpr.text:
                    ttype = desttrait.trait_type
                    if not ttype:
                        ttype = desttrait
                    if ttype.validate:
                        ttype.validate(destcomp, destvarname, srcexpr.evaluate())
                    else:
                        # no validate function on destination trait. Most likely
                        # it's a property trait.  No way to validate without
                        # unknown side effects. Have to wait until later when
                        # data actually gets passed via the connection.
                        pass

        self.exprs[src] = srcexpr
        self.exprs[dest] = destexpr
        self.dests.setdefault(src, []).append(dest)
        self.srcs[dest] = src

        self.depgraph.connect(src, dest, scope)

    def find_referring_exprs(self, name):
        """Returns a set of expression strings that reference the given name, which
        can refer to either a variable or a component.
        """
        return set([node.text for node in self.exprs.values() if node.refers_to(name)])

    def disconnect(self, srcpath, destpath=None):
        """Disconnect the given expressions/variables/components."""

        self.depgraph.disconnect(srcpath, destpath)

        if destpath is None:
            if srcpath in self.exprs:
                return self._remove_expr(srcpath)
            else:
                return self.remove(srcpath)

        if srcpath in self.exprs and destpath in self.exprs:
            return self._remove_expr(destpath) # only remove dest. src will be removed if it has no more dests
        else:  # assume they're disconnecting two variables, so find connected exprs that refer to them
            conns = self.remove(destpath)
            for expr in self.find_referring_exprs(srcpath):
                conns.extend(self._remove_expr(expr))
            return conns

    def check_connect(self, src, dest):
        """Check validity of connecting a source expression to a 
        destination expression.
        """

        if self.get_source(dest) is not None:
            raise RuntimeError("'%s' is already connected to source '%s'" % (dest, self.get_source(dest)))

        destexpr = ConnectedExprEvaluator(dest, getter='get_wrapped_attr',
                                          is_dest=True)
        srcexpr = ConnectedExprEvaluator(src, getter='get_wrapped_attr')

        srccomps = srcexpr.get_referenced_compnames()
        destcomps = destexpr.get_referenced_compnames()

        if destcomps and destcomps.pop() in srccomps:
            raise RuntimeError("'%s' and '%s' refer to the same component." % (src, dest))

