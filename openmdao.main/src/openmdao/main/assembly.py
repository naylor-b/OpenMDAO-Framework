""" Class definition for Assembly. """


#public symbols
__all__ = ['Assembly', 'set_as_top']

from fnmatch import fnmatch
import re
import sys
import threading
import traceback
from itertools import chain

from numpy import ndarray

# pylint: disable=E0611,F0401
import networkx as nx
from networkx.algorithms.components import strongly_connected_components
from networkx.algorithms.dag import is_directed_acyclic_graph

from openmdao.main.mpiwrap import MPI

from openmdao.main.exceptions import NoFlatError
from openmdao.main.interfaces import implements, IAssembly, IDriver, \
                                     IComponent, IContainer, \
                                     ICaseRecorder, IHasParameters
from openmdao.main.mp_support import has_interface
from openmdao.main.container import _copydict
from openmdao.main.component import Component, Container
from openmdao.main.variable import Variable
from openmdao.main.vartree import VariableTree
from openmdao.main.datatypes.api import List, Slot, Bool, VarTree
from openmdao.main.driver import Driver
from openmdao.main.rbac import rbac
from openmdao.main.mp_support import is_instance
from openmdao.main.printexpr import eliminate_expr_ws
from openmdao.main.expreval import ExprEvaluator, ConnectedExprEvaluator
from openmdao.main.exprmapper import ExprMapper
from openmdao.main.pseudocomp import PseudoComponent, UnitConversionPComp
from openmdao.main.array_helpers import is_differentiable_var, get_val_and_index, \
                                        get_flattened_index, \
                                        get_var_shape, flattened_size
from openmdao.main.depgraph import DependencyGraph, all_comps, \
                                   list_driver_connections, \
                                   simple_node_iter, \
                                   is_boundary_node
from openmdao.main.systems import SerialSystem, _create_simple_sys

from openmdao.util.graph import list_deriv_vars, base_var, fix_single_tuple
from openmdao.util.log import logger
from openmdao.util.debug import strict_chk_config

from openmdao.util.graphplot import _clean_graph
from networkx.readwrite import json_graph
import json


_iodict = {'out': 'output', 'in': 'input'}

_missing = object()

__has_top__ = False
__toplock__ = threading.RLock()


def set_as_top(cont, first_only=False):
    """Specifies that the given Container is the top of a Container hierarchy.
    If first_only is True, then only set it as a top if a global
    top doesn't already exist.
    """
    global __has_top__
    with __toplock__:
        if __has_top__ is False and isinstance(cont, Assembly):
            __has_top__ = True
        elif first_only:
            return cont
    if cont._call_cpath_updated:
        cont.cpath_updated()
    return cont


class PassthroughTrait(Variable):
    """A trait that can use another trait for validation, but otherwise is
    just a trait that lives on an Assembly boundary and can be connected
    to other traits within the Assembly.
    """

    def validate(self, obj, name, value):
        """Validation for the PassThroughTrait."""
        if self.validation_trait:
            return self.validation_trait.validate(obj, name, value)
        return value


class PassthroughProperty(Variable):
    """Replacement for PassthroughTrait when the target is a proxy/property
    trait. PassthroughTrait would get a core dump while pickling.
    """
    def __init__(self, target_trait, **metadata):
        self._trait = target_trait
        self._vals = {}
        super(PassthroughProperty, self).__init__(**metadata)

    def get(self, obj, name):
        v = self._vals.get(obj, _missing)
        if v is not _missing:
            return v.get(name, self._trait.default_value)
        else:
            return self._trait.default_value

    def set(self, obj, name, value):
        if obj not in self._vals:
            self._vals[obj] = {}
        old = self.get(obj, name)
        if value != old:
            self._vals[obj][name] = self._trait.validate(obj, name, value)
            obj.trait_property_changed(name, old, value)


class RecordingOptions(VariableTree):
    """Container for options that control case recording. """

    save_problem_formulation = Bool(True, desc='Save problem formulation '
                                               '(parameters, constraints, etc.)')

    includes = List(['*'], desc='Patterns for variables to include in recording')

    excludes = List([], desc='Patterns for variables to exclude from recording '
                             '(processed after includes')


class Assembly(Component):
    """This is a container of Components. It understands how to connect inputs
    and outputs between its children.  When executed, it runs the top level
    Driver called 'driver'.
    """

    implements(IAssembly)

    driver = Slot(IDriver, allow_none=True,
                    desc="The top level Driver that manages execution of "
                    "this Assembly.")

    recorders = List(Slot(ICaseRecorder, required=False),
                     desc='Case recorders for iteration data'
                          ' (only valid at top level).')

    recording_options = VarTree(RecordingOptions(), iotype='in',
                    framework_var=True, deriv_ignore=True,
                    desc='Case recording options (only valid at top level).')

    def __init__(self):

        super(Assembly, self).__init__()

        self._pseudo_count = 0  # counter for naming pseudocomps
        self._pre_driver = None
        self._derivs_required = False
        self._unexecuted = []
        self._var_meta = {}

        # data dependency graph. Includes edges for data
        # connections as well as for all driver parameters and
        # constraints/objectives.  This is the starting graph for
        # all later transformations.
        self._depgraph = None #DependencyGraph()
        self._reduced_graph = nx.DiGraph()

        # for name, trait in self.class_traits().items():
        #     if trait.iotype:  # input or output
        #         self._depgraph.add_boundary_var(self, name, iotype=trait.iotype)

        self._exprmapper = ExprMapper()
        self.J_input_keys = None
        self.J_output_keys = None

        # default Driver executes its workflow once
        self.add('driver', Driver())

        # we're the top Assembly only if we're the first instantiated
        set_as_top(self, first_only=True)

        # Assemblies automatically figure out their own derivatives, so
        # any boundary vars that are unconnected should be zero.
        self.missing_deriv_policy = 'assume_zero'

        self.add('recording_options', RecordingOptions())

    def _pre_execute(self):
        """Prepares for execution by calling various initialization methods
        if necessary.

        Overrides of this function must call this version.
        """
        super(Assembly, self)._pre_execute()

        if self._new_config:
            if self.parent is None:
                self._setup()
            self._new_config = False
            
        if self.parent is None:
            self.configure_recording(self.recording_options)

    @property
    def _top_driver(self):
        if self._pre_driver:
            return self._pre_driver
        return self.driver

    @rbac(('owner', 'user'))
    def set_itername(self, itername, seqno=0):
        """
        Set current 'iteration coordinates'. Overrides :class:`Component`
        to propagate to driver, and optionally set the initial count in the
        driver's workflow. Setting the initial count is typically done by
        :class:`CaseIterDriverBase` on a remote top level assembly.

        itername: string
            Iteration coordinates.

        seqno: int
            Initial execution count for driver's workflow.
        """
        super(Assembly, self).set_itername(itername)
        self._top_driver.set_itername(itername)
        if seqno:
            self._top_driver.workflow.set_initial_count(seqno)

    def find_referring_connections(self, name):
        """Returns a list of connections where the given name is referred
        to either in the source or the destination.
        """
        exprset = set(self._exprmapper.find_referring_exprs(name))
        return [(u, v) for u, v
                       in self._depgraph.list_connections(show_passthrough=True)
                                        if u in exprset or v in exprset]

    def find_in_workflows(self, name):
        """Returns a list of tuples of the form (workflow, index) for all
        workflows in the scope of this Assembly that contain the given
        component name.
        """
        wflows = []
        for item in self.list_containers():
            if item != name:
                obj = self.get(item)
                if isinstance(obj, Driver) and name in obj.workflow._explicit_names:
                    wflows.append((obj.workflow, obj.workflow._explicit_names.index(name)))
        return wflows

    def _add_after_parent_set(self, name, obj):
        pass
        # if has_interface(obj, IComponent):
        #     self._depgraph.add_component(name, obj)
        # elif has_interface(obj, IContainer) and name not in self._depgraph:
        #     t = self.get_trait(name)
        #     if t is not None:
        #         io = t.iotype
        #         if io:
        #             # since we just removed this container and it was
        #             # being used as an io variable, we need to put
        #             # it back in the dep graph
        #             self._depgraph.add_boundary_var(self, name, iotype=io)

    def add_trait(self, name, trait, refresh=True):
        """Overrides base definition of *add_trait* in order to
        update the depgraph.
        """
        super(Assembly, self).add_trait(name, trait, refresh)
        # if trait.iotype and name not in self._depgraph:
        #     self._depgraph.add_boundary_var(self, name,
        #                                     iotype=trait.iotype)

    def rename(self, oldname, newname):
        """Renames a child of this object from oldname to newname."""
        self._check_rename(oldname, newname)
        #conns = self.find_referring_connections(oldname)
        conns = self._exprmapper.find_referring_edges(oldname)
        wflows = self.find_in_workflows(oldname)

        obj = self.remove(oldname)
        obj.name = newname
        self.add(newname, obj)

        # oldname has now been removed from workflows, but newname may be in the
        # wrong location, so force it to be at the same index as before removal
        for wflow, idx in wflows:
            wflow.remove(newname)
            wflow.add(newname, idx)

        old_rgx = re.compile(r'(\W?)%s.' % oldname)

        # recreate all of the broken connections after translating
        # oldname to newname
        for u, v in conns:
            self.connect(re.sub(old_rgx, r'\g<1>%s.' % newname, u),
                         re.sub(old_rgx, r'\g<1>%s.' % newname, v))

    def replace(self, target_name, newobj):
        """Replace one object with another, attempting to mimic the
        inputs and connections of the replaced object as much as possible.
        """
        tobj = getattr(self, target_name)

        if not tobj:
            self.add(target_name, newobj)
            return

        # Save existing driver references.
        refs = {}
        if has_interface(tobj, IComponent):
            for cname in self.list_containers():
                obj = getattr(self, cname)
                if obj is not tobj and has_interface(obj, IDriver):
                    refs[cname] = obj.get_references(target_name)
                    #obj.remove_references(target_name)

        if hasattr(newobj, 'mimic'):
            try:
                # this should copy inputs, delegates and set name
                newobj.mimic(tobj)
            except Exception:
                self.reraise_exception("Couldn't replace '%s' of type %s with"
                                       " type %s"
                                       % (target_name, type(tobj).__name__,
                                          type(newobj).__name__), sys.exc_info())

        exprconns = [(u, v) for u, v in self._exprmapper.list_connections(self)
                                 if '_pseudo_' not in u and '_pseudo_' not in v]
        conns = self.find_referring_connections(target_name)
        wflows = self.find_in_workflows(target_name)

        # Assemblies sometimes create inputs and outputs in their configure()
        # function, so call it early if possible
        if self._call_cpath_updated is False and isinstance(obj, Container):
            newobj.parent = self
            newobj.name = target_name
            newobj.cpath_updated()

        # check that all connected vars exist in the new object
        req_vars = set([u.split('.', 1)[1].split('[', 1)[0]
                        for u, v in conns if u.startswith(target_name+'.')])
        req_vars.update([v.split('.', 1)[1].split('[', 1)[0]
                         for u, v in conns if v.startswith(target_name+'.')])
        missing = [v for v in req_vars if not newobj.contains(v)]
        if missing:
            self._logger.warning("the following variables are connected to "
                                 "other components but are missing in "
                                 "the replacement object: %s" % missing)

        # remove expr connections
        for u, v in exprconns:
            self.disconnect(u, v)

        # remove any existing connections to replacement object
        if has_interface(newobj, IComponent):
            self.disconnect(newobj.name)

        self.add(target_name, newobj)  # this will remove the old object
                                       # and any connections to it

        # recreate old connections
        for u, v in exprconns:
            try:
                self.connect(u, v)
            except Exception as err:
                self._logger.warning("Couldn't connect '%s' to '%s': %s",
                                     u, v, err)

        # Restore driver references.
        for dname, _refs in refs.items():
            drv = getattr(self, dname)
            drv.remove_references(target_name)
            drv.restore_references(_refs)

        # add new object (if it's a Component) to any
        # workflows where target was
        if has_interface(newobj, IComponent):
            for wflow, idx in wflows:
                wflow.add(target_name, idx)

    def remove(self, name):
        """Remove the named container object from this assembly
        and remove it from its workflow(s) if it's a Component
        or pseudo component.
        """
        obj = getattr(self, name)
        if has_interface(obj, IComponent) or isinstance(obj, PseudoComponent):
            for cname in self.list_containers():
                cobj = getattr(self, cname)
                if isinstance(cobj, Driver) and cobj is not obj:
                    cobj.remove_references(name)
            self.disconnect(name)
        elif name in self.list_inputs() or name in self.list_outputs():
            self.disconnect(name)

        # if has_interface(obj, IDriver):
        #     for pcomp in obj.list_pseudocomps():
        #         if pcomp in self._depgraph:
        #             self._depgraph.remove(pcomp)

        # if name in self._depgraph:
        #     self._depgraph.remove(name)

        return super(Assembly, self).remove(name)

    def create_passthrough(self, pathname, alias=None):
        """Creates a PassthroughTrait that uses the trait indicated by
        pathname for validation, adds it to self, and creates a connection
        between the two. If alias is *None,* the name of the alias trait will
        be the last entry in its pathname. The trait specified by pathname
        must exist.
        """
        parts = pathname.split('.')
        if alias:
            newname = alias
        else:
            newname = parts[-1]

        if newname in self.__dict__:
            self.raise_exception("'%s' already exists" % newname, KeyError)
        if len(parts) < 2:
            self.raise_exception('destination of passthrough must be a dotted'
                                 ' path', NameError)
        comp = self
        for part in parts[:-1]:
            try:
                comp = getattr(comp, part)
            except AttributeError:
                trait = None
                break
        else:
            trait = comp.get_trait(parts[-1])
            iotype = comp.get_iotype(parts[-1])

        if trait:
            ttype = trait.trait_type
            if ttype is None:
                ttype = trait
        else:
            if not self.contains(pathname):
                self.raise_exception("the variable named '%s' can't be found" %
                                     pathname, KeyError)
            iotype = self.get_metadata(pathname, 'iotype')

        if trait is not None and not trait.validate:
            # no validate function, so just don't use trait for validation
            trait = None

        metadata = self.get_metadata(pathname)
        metadata['target'] = pathname
        metadata['default_value'] = trait.trait_type.default_value
        # PassthroughTrait to a trait with get/set methods causes a core dump
        # in Traits (at least through 3.6) while pickling.
        if "validation_trait" in metadata:
            if metadata['validation_trait'].get is None:
                newtrait = PassthroughTrait(**metadata)
            else:
                newtrait = PassthroughProperty(metadata['validation_trait'],
                                               **metadata)
        elif trait and ttype.get:
            newtrait = PassthroughProperty(ttype, **metadata)
        else:
            newtrait = PassthroughTrait(validation_trait=trait, **metadata)
        self.add_trait(newname, newtrait)

        # Copy trait value according to 'copy' attribute in the trait
        val = self.get(pathname)

        ttype = trait.trait_type
        if ttype.copy:
            # Variable trees need to point to a new parent.
            # Also, let's not deepcopy the outside universe
            if isinstance(val, Container):
                val_copy = val.copy()
                val_copy.parent = self
                val = val_copy
                val.name = newname
            else:
                val = _copydict[ttype.copy](val)

        setattr(self, newname, val)

        try:
            if iotype == 'in':
                self.connect(newname, pathname)
            else:
                self.connect(pathname, newname)
        except RuntimeError:
            info = sys.exc_info()
            self.remove(newname)
            raise info[0], info[1], info[2]

        return newtrait

    @rbac(('owner', 'user'))
    def check_config(self, strict=False):
        """
        Verify that this component and all of its children are properly
        configured to execute. This function is called prior the first
        component execution.  If strict is True, any warning or error
        should raise an exception.

        If you override this function to do checks specific to your class,
        you must call this function.
        """

        super(Assembly, self).check_config(strict=strict)
        self._check_input_collisions()
        self._check_unset_req_vars()
        self._check_unexecuted_comps(strict=strict)
        
    def _check_input_collisions(self):
        graph = self._depgraph
        dests = set([v for u, v in self.list_connections() if 'drv_conn_ext' not in graph[u][v]])
        allbases = set([base_var(graph, v) for v in dests])
        unconnected_bases = allbases - dests
        connected_bases = allbases - unconnected_bases

        collisions = []
        for drv in chain([self._top_driver],
                          self._top_driver.subdrivers(recurse=True)):
            if has_interface(drv, IHasParameters):
                for target in drv.list_param_targets():
                    tbase = base_var(graph, target)
                    if target == tbase:  # target is a base var
                        if target in allbases:
                            collisions.append("%s in %s"
                                              % (target, drv.get_pathname()))
                    else:  # target is a sub var
                        if target in dests or tbase in connected_bases:
                            collisions.append("%s in %s"
                                              % (target, drv.get_pathname()))

        if collisions:
            self.raise_exception("The following parameters collide with"
                                 " connected inputs: %s" % ','.join(collisions),
                                 RuntimeError)

    def _check_unexecuted_comps(self, strict):
        self._unexecuted = []
        pre = []
        cgraph = self._depgraph.component_graph()
        wfcomps = set([c.name for c in self.driver.iteration_set()])
        wfcomps.add('driver')
        diff = set(cgraph.nodes()) - wfcomps
        self._pre_driver = None
        if diff:
            msg = "The following components are not in any workflow but " \
                  "are needed by other workflows"
            if strict_chk_config(strict):
                errfunct = self.raise_exception
            else:
                errfunct = self._logger.warning
                msg += ", so they will be executed once per execution of " \
                       "this Assembly"

            out_edges = nx.edge_boundary(cgraph, diff)
            pre = [u for u, v in out_edges]
            post = diff - set(pre)

            if pre:
                msg += ": %s" % pre
                errfunct(msg)

            if post:
                errfunct("The following components are not in any workflow"
                         " and WILL NOT EXECUTE: %s" % list(diff))
                self._unexecuted = list(post)
                
        return pre

    def _check_unset_req_vars(self):
        """Find 'required' variables that have not been set."""
        graph = self._depgraph
        for name in chain(all_comps(graph),
                          graph.get_boundary_inputs(),
                          graph.get_boundary_outputs()):
            obj = getattr(self, name)
            if has_interface(obj, IContainer):
                for vname in obj.get_req_default(self.trait(name).required):
                    # each var must be connected, otherwise value will not
                    # be set to a non-default value
                    base = base_var(graph, vname)
                    indeg = graph.in_degree(base)
                    io = graph.node[base]['iotype']
                    if (io == 'in' and indeg < 1) or \
                       (io == 'state' and indeg < 2):
                        self.raise_exception("required variable '%s' was"
                                             " not set" % vname, RuntimeError)

    def _check_connect(self, src, dest):
        """Check validity of connecting a source expression to a destination
        expression.
        """

        if self._exprmapper.get_source(dest) is not None:
            self.raise_exception("'%s' is already connected to source '%s'" %
                                  (dest, self._exprmapper.get_source(dest)), RuntimeError)

        destexpr = ConnectedExprEvaluator(dest, self, is_dest=True)
        srcexpr = ConnectedExprEvaluator(src, self,
                                         getter='get_attr_w_copy')

        srccomps = srcexpr.get_referenced_compnames()
        destcomps = list(destexpr.get_referenced_compnames())

        if destcomps and destcomps[0] in srccomps:
            raise RuntimeError("'%s' and '%s' refer to the same component."
                               % (src, dest))

        return srcexpr, destexpr

    @rbac(('owner', 'user'))
    def connect(self, src, dest):
        """Connect one source variable or expression to one or more
        destination variables.

        src: str
            Source expression string.

        dest: str or list(str)
            Destination variable string(s).
        """
        src = eliminate_expr_ws(src)

        if isinstance(dest, basestring):
            dest = (dest,)
        for dst in dest:
            dst = eliminate_expr_ws(dst)
            try:
                self._connect(src, dst)
            except Exception:
                self.reraise_exception("Can't connect '%s' to '%s'" % (src, dst),
                                        sys.exc_info())

    def _connect(self, src, dest):
        """Handle one connection destination. This should only be called via
        the connect() function, never directly.
        """

        # Among other things, check if already connected.
        # srcexpr, destexpr, pcomp_type = \
        #            self._exprmapper.check_connect(src, dest, self)
        #srcexpr, destexpr = self._check_connect(src, dest)

        # if pcomp_type is not None:
        #     if pcomp_type == 'units':
        #         pseudocomp = UnitConversionPComp(self, srcexpr, destexpr,
        #                                          pseudo_type=pcomp_type)
        #     else:
        #         pseudocomp = PseudoComponent(self, srcexpr, destexpr,
        #                                      pseudo_type=pcomp_type)
        #     self.add(pseudocomp.name, pseudocomp)
        #     pseudocomp.make_connections(self)
        # else:
        #     pseudocomp = None
        #     self._depgraph.check_connect(src, dest)
        #     dcomps = destexpr.get_referenced_compnames()
        #     scomps = srcexpr.get_referenced_compnames()
        #     for dname in dcomps:
        #         if dname in scomps:
        #             self.raise_exception("Can't connect '%s' to '%s'. Both"
        #                                  " refer to the same component." %
        #                                  (src, dest), RuntimeError)
        #     for cname in chain(dcomps, scomps):
        #         comp = getattr(self, cname)
        #         if has_interface(comp, IComponent):
        #             comp.config_changed(update_parent=False)

        #     for vname in chain(srcexpr.get_referenced_varpaths(copy=False),
        #                        destexpr.get_referenced_varpaths(copy=False)):
        #         if not self.contains(vname):
        #             self.raise_exception("Can't find '%s'" % vname,
        #                                  AttributeError)

            #self._depgraph.connect(self, src, dest)

        self._exprmapper.connect(self, src, dest)

        # dest = destexpr.evaluate()
        # if isinstance(dest, ndarray) and dest.size == 0:
        #     destexpr.set(srcexpr.evaluate(), self)

        self.config_changed(update_parent=False)

    @rbac(('owner', 'user'))
    def disconnect(self, varpath, varpath2=None):
        """If varpath2 is supplied, remove the connection between varpath and
        varpath2. Otherwise, if varpath is the name of a trait, remove all
        connections to/from varpath in the current scope. If varpath is the
        name of a Component, remove all connections from all of its inputs
        and outputs.
        """
        try:
            cnames = ExprEvaluator(varpath, self).get_referenced_compnames()
            if varpath2 is not None:
                cnames.update(ExprEvaluator(varpath2, self).get_referenced_compnames())
            boundary_vars = self.list_inputs() + self.list_outputs()
            for cname in cnames:
                if cname not in boundary_vars:
                    getattr(self, cname).config_changed(update_parent=False)

            to_remove, pcomps = self._exprmapper.disconnect(self, varpath, varpath2)

            #graph = self._depgraph

            #if to_remove:
                #for u, v in graph.list_connections():
                    #if (u, v) in to_remove:
                        #graph.disconnect(u, v)
                        #to_remove.remove((u, v))

            #if to_remove:  # look for pseudocomp expression connections
                #for node, data in graph.nodes_iter(data=True):
                    #if 'srcexpr' in data:
                        #for u, v in to_remove:
                            #if data['srcexpr'] == u or data['destexpr'] == v:
                                #pcomps.add(node)

            #for name in pcomps:
                #if '_pseudo_' not in varpath:
                    #self.remove(name)
                #else:
                    #try:
                        #self.remove_trait(name)
                    #except Exception:
                        #pass
                #try:
                    #graph.remove(name)
                #except (KeyError, nx.exception.NetworkXError):
                    #pass
        finally:
            self.config_changed(update_parent=False)

    def config_changed(self, update_parent=True):
        """Call this whenever the configuration of this Component changes,
        for example, children are added or removed, connections are made
        or removed, etc.
        """
        super(Assembly, self).config_changed(update_parent)

        # drivers must tell workflows that config has changed because
        # dependencies may have changed
        for name in self.list_containers():
            cont = getattr(self, name)
            if isinstance(cont, Driver):
                cont.config_changed(update_parent=False)

        self._pre_driver = None
        self.J_input_keys = self.J_output_keys = None
        self._system = None

    def _set_failed(self, path, value):
        parts = path.split('.', 1)
        if len(parts) > 1:
            obj = getattr(self, parts[0])
            if isinstance(obj, PseudoComponent):
                obj.set(parts[1], value)

    def execute(self):
        """Runs driver and updates our boundary variables."""
        for system in self._system.local_subsystems():
            system.pre_run()
        self._system.run(self.itername, case_uuid=self._case_uuid)

    def configure_recording(self, recording_options=None):
        """Called at start of top-level run to configure case recording.
        Returns set of paths for changing inputs."""
        if self.parent is None:
            if self.recorders:
                recording_options = self.recording_options
                for recorder in self.recorders:
                    recorder.startup()
            else:
                recording_options = None

        if recording_options:
            includes = recording_options.includes
            excludes = recording_options.excludes
            save_problem_formulation = recording_options.save_problem_formulation
        else:
            includes = excludes = save_problem_formulation = None

        # Determine (changing) inputs and outputs to record
        inputs = set()
        constants = {}
        for name in self.list_containers():
            obj = getattr(self, name)
            if has_interface(obj, IDriver, IAssembly):
                inps, consts = obj.configure_recording(recording_options)
                inputs.update(inps)
                constants.update(consts)

        # If nothing to record, return after configuring workflows.
        if not save_problem_formulation and not includes:
            return (inputs, constants)

        # Locate top level assembly.
        top = self
        while top.parent:
            top = top.parent
        prefix_drop = len(top.name)+1 if top.name else 0

        # Determine constant inputs.
        objs = [self]
        objs.extend(getattr(self, name) for name in self.list_containers())
        for obj in objs:
            if has_interface(obj, IComponent):
                prefix = obj.get_pathname()[prefix_drop:]
                if prefix:
                    prefix += '.'

                in_names = obj.list_inputs()
                if obj.parent is not None:
                    conn = obj.parent.connected_inputs(obj.name)
                    for name in conn:
                        obj_name, _, in_name = name.partition('.')
                        if in_name in in_names:
                            in_names.remove(in_name)

                for name in in_names:
                    path = prefix+name
                    if path in inputs:
                        continue  # Changing input.

                    record_constant = False
                    for pattern in includes:
                        if fnmatch(path, pattern):
                            record_constant = True

                    if record_constant:
                        for pattern in excludes:
                            if fnmatch(path, pattern):
                                record_constant = False

                    if record_constant:
                        val = getattr(obj, name)
                        if isinstance(val, VariableTree):
                            for path, val in self._expand_tree(path, val):
                                constants[path] = val
                        else:
                            constants[path] = val

        # Record constant inputs.
        if self.parent is None:
            for recorder in self.recorders:
                recorder.record_constants(constants)

        return (inputs, constants)

    def record_configuration(self):
        """ record model configuration without running the model
        """
        top = self
        while top.parent:
            top = top.parent
        top._setup()
        self.configure_recording()
        for recorder in self.recorders:
            recorder.close()

    @rbac(('owner', 'user'))
    def connected_inputs(self, name):
        """Helper for :meth:`configure_recording`."""
        return self._depgraph.list_inputs(name, connected=True)

    def _expand_tree(self, path, tree):
        """Return list of ``(path, value)`` with :class:`VariableTree`
        expanded."""
        path += '.'
        result = []
        for name, val in tree._items(set()):
            if isinstance(val, VariableTree):
                result.extend(self._expand_tree(path+name, val))
            else:
                result.append((path+name, val))
        return result

    def stop(self):
        """Stop the calculation."""
        self._top_driver.stop()

    @rbac(('owner', 'user'))
    def child_config_changed(self, child, adding=True, removing=True):
        """A child has changed its input lists and/or output lists,
        so we need to update the graph.
        """
        ## if this is called during __setstate__, self._depgraph may not
        ## exist yet, so...
        #if hasattr(self, '_depgraph'):
            #self._depgraph.child_config_changed(child, adding=adding,
                                                #removing=removing)

    def list_connections(self, show_passthrough=True,
                               visible_only=False,
                               show_expressions=False):
        """Return a list of tuples of the form (outvarname, invarname).
        """
        return self._exprmapper.list_connections(self, show_passthrough=show_passthrough,
                                                 visible_only=visible_only)
        #conns = self._depgraph.list_connections(show_passthrough=show_passthrough)
        #if visible_only:
            #newconns = []
            #for u, v in conns:
                #if u.startswith('_pseudo_'):
                    #pcomp = getattr(self, u.split('.', 1)[0])
                    #newconns.extend(pcomp.list_connections(is_hidden=True,
                                     #show_expressions=show_expressions))
                #elif v.startswith('_pseudo_'):
                    #pcomp = getattr(self, v.split('.', 1)[0])
                    #newconns.extend(pcomp.list_connections(is_hidden=True,
                                     #show_expressions=show_expressions))
                #else:
                    #newconns.append((u, v))
            #return newconns
        #return conns

    @rbac(('owner', 'user'))
    def child_run_finished(self, childname, outs=None):
        """Called by a child when it completes its run() function."""
        self._depgraph.child_run_finished(childname, outs)

    def exec_counts(self, compnames):
        return [getattr(self, c).exec_count for c in compnames]

    def check_gradient(self, name=None, inputs=None, outputs=None,
                       stream=sys.stdout, mode='auto',
                       fd_form='forward', fd_step=1.0e-6,
                       fd_step_type='absolute'):

        """Compare the OpenMDAO-calculated gradient with one calculated
        by straight finite-difference. This provides the user with a way
        to validate his derivative functions (apply_deriv and provideJ.)

        name: (optional) str
            If provided, specifies the name of a Driver or Component to
            calculate the gradient for.  If name specifies a Driver,
            the inputs used to calculate the gradient will be generated
            from the parameters of the Driver, and the outputs will be
            generated from the constraints and objectives of the Driver.
            If name specifies a Component, the inputs and outputs of that
            Component will be used to calculate the gradient.

        inputs: (optional) iter of str or None
            Names of input variables. The calculated gradient will be
            the matrix of values of the output variables with respect
            to these input variables. If no value is provided for inputs,
            they will be determined based on the 'name' argument.
            If the inputs are not specified and name is not specified,
            then they will be generated from the parameters of
            the object named 'driver'.

        outputs: (optional) iter of str or None
            Names of output variables. The calculated gradient will be
            the matrix of values of these output variables with respect
            to the input variables. If no value is provided for outputs,
            they will be determined based on the 'name' argument.
            If the outputs are not specified and name is not specified,
            then they will be generated from the objectives and constraints
            of the object named 'driver'.

        stream: (optional) file-like object, str, or None
            Where to write to, default stdout. If a string is supplied,
            that is used as a filename. If None, no output is written.

        mode: (optional) str or None
            Set to 'forward' for forward mode, 'adjoint' for adjoint mode,
            or 'auto' to let OpenMDAO determine the correct mode.
            Defaults to 'auto'.

        fd_form: str
            Finite difference mode. Valid choices are 'forward', 'adjoint' ,
            'central'. Default is 'forward'

        fd_step: float
            Default step_size for finite difference. Default is 1.0e-6.

        fd_step_type: str
            Finite difference step type. Set to 'absolute' or 'relative'.
            Default is 'absolute'.

        Returns the finite difference gradient, the OpenMDAO-calculated
        gradient, a list of the gradient names, and a list of suspect
        inputs/outputs.
        """
        driver = self.driver
        obj = None

        # tuples cause problems.
        if inputs:
            inputs = list(inputs)
        if outputs:
            outputs = list(outputs)

        if inputs and outputs:
            if name:
                logger.warning("The 'inputs' and 'outputs' args were specified"
                               " to check_gradient, so the 'name' arg (%s) is"
                               " ignored.", name)
        elif not name:
            # we're missing either inputs or outputs, so we need a name
            name = 'driver'

        if name:
            obj = getattr(self, name, None)
            if obj is None:
                self.raise_exception("Can't find object named '%s'." % name)
            if has_interface(obj, IDriver):
                driver = obj

        # fill in missing inputs or outputs using the object specified by 'name'
        if not inputs:
            if has_interface(obj, IDriver):
                pass  # workflow.check_gradient can pull inputs from driver
            elif has_interface(obj, IAssembly):
                inputs = ['.'.join((obj.name, inp))
                          for inp in obj.list_inputs()
                                  if is_differentiable_var(inp, obj)]
                inputs = sorted(inputs)
            elif has_interface(obj, IComponent):
                inputs = ['.'.join((obj.name, inp))
                          for inp in list_deriv_vars(obj)[0]]
                inputs = sorted(inputs)
            else:
                self.raise_exception("Can't find any inputs for generating"
                                     " gradient.")
        if not outputs:
            if has_interface(obj, IDriver):
                pass  # workflow.check_gradient can pull outputs from driver
            elif has_interface(obj, IAssembly):
                outputs = ['.'.join((obj.name, out))
                           for out in obj.list_outputs()
                                   if is_differentiable_var(out, obj)]
                outputs = sorted(outputs)
            elif has_interface(obj, IComponent):
                outputs = ['.'.join((obj.name, outp))
                          for outp in list_deriv_vars(obj)[1]]
                outputs = sorted(outputs)
            else:
                self.raise_exception("Can't find any outputs for generating"
                                     " gradient.")

        if not has_interface(obj, IDriver) and (not inputs or not outputs):
            msg = 'Component %s has no analytic derivatives.' % obj.name
            self.raise_exception(msg)

        base_fd_form = driver.gradient_options.fd_form
        base_fd_step = driver.gradient_options.fd_step
        base_fd_step_type = driver.gradient_options.fd_step_type

        driver.gradient_options.fd_form = fd_form
        driver.gradient_options.fd_step = fd_step
        driver.gradient_options.fd_step_type = fd_step_type

        try:
            result = driver.workflow.check_gradient(inputs=inputs,
                                                    outputs=outputs,
                                                    stream=stream,
                                                    mode=mode)
        finally:
            driver.gradient_options.fd_form = base_fd_form
            driver.gradient_options.fd_step = base_fd_step
            driver.gradient_options.fd_step_type = base_fd_step_type

        return result

    def list_components(self):
        ''' List the components in the assembly.
        '''
        names = [name for name in self.list_containers()
                     if isinstance(self.get(name), Component)]
        return names

    @rbac(('owner', 'user'))
    def new_pseudo_name(self):
        name = "_pseudo_%d" % self._pseudo_count
        self._pseudo_count += 1
        return name

    def get_graph(self, components_only=False, format='json'):
        ''' returns cleaned up graph data in the selected format

            components_only: (optional) boolean
                if True, only components will be included in the graph
                otherwise the full dependency graph will be returned

            format: (optional) string
                json - returns serialized graph data in JSON format
                svg  - returns scalable vector graphics rendition of graph
        '''
        if components_only:
            graph = self._depgraph.component_graph()
        else:
            graph = _clean_graph(self._depgraph)

        if format.lower() == 'json':
            graph_data = json_graph.node_link_data(graph)
            return json.dumps(graph_data)
        elif format.lower() == 'svg':
            agraph = nx.to_agraph(graph)
            return agraph.draw(format='svg', prog='dot')
        else:
            self.raise_exception("'%s' is not a supported graph data format" % format)

    def _repr_svg_(self):
        """ Returns an SVG representation of this Assembly's dependency graph
            (if pygraphviz is not available, returns an empty string)
        """
        try:
            import pygraphviz
            return self.get_graph(components_only=True, format='svg')
        except ImportError:
            return ''

    def get_depgraph(self):
        return self._depgraph

    def get_reduced_graph(self):
        return self._reduced_graph

    def get_comps(self):
        """Returns a list of all of objects contained in this
        Assembly implementing the IComponent interface.
        """
        conts = [getattr(self, n) for n in sorted(self.list_containers())]
        return [c for c in conts if has_interface(c, IComponent)]

    def get_system(self):
        return self._system

    @rbac(('owner', 'user'))
    def setup_systems(self):
        rgraph = self._reduced_graph

        # store metadata (size, etc.) for all relevant vars
        self._get_all_var_metadata(self._reduced_graph)

        # create systems for all simple components
        for node, data in rgraph.nodes_iter(data=True):
            if 'comp' in data:
                data['system'] = _create_simple_sys(self, rgraph, node)

        for name in self._unexecuted:
            comp = getattr(self, name)
            if has_interface(comp, IDriver) or has_interface(comp, IAssembly):
                comp.setup_systems()

        self._top_driver.setup_systems()

        # copy the reduced graph
        rgraph = rgraph.subgraph(rgraph.nodes_iter())
        rgraph.collapse_subdrivers([], [self._top_driver])

        drvname = self._top_driver.name

        if len(rgraph) > 1:
            self._system = SerialSystem(self, rgraph, rgraph.component_graph(),
                                        self.name+'._inner_asm')
            # see if there's a driver cycle (happens when driver has params and
            # constraints/objectives that are boundary vars.)
            # FIXME: if we modify the graph to have to/from edges between a driver and
            # all of its workflow comps, then use strongly connected components to
            # determine full iteration sets, this will never happen.
            for strong in strongly_connected_components(rgraph):
                if drvname in strong:
                    if len(strong) > 1:
                        # break driver input edge
                        for p in rgraph.predecessors(drvname):
                            if p in strong:
                                rgraph.remove_edge(p, drvname)
                                if is_directed_acyclic_graph(rgraph):
                                    break
                    break
            self._system.set_ordering(nx.topological_sort(rgraph), {})
        else:
            # TODO: if top driver has no params/constraints, possibly
            # remove driver system entirely and just go directly to workflow
            # system...
            self._system = rgraph.node[self._top_driver.name]['system']

    @rbac(('owner', 'user'))
    def get_req_cpus(self):
        """Return requested_cpus"""
        return self._top_driver.get_req_cpus()

    def setup_communicators(self, comm):
        self._system.setup_communicators(comm)

    def setup_variables(self):
        self._system.setup_variables()

    def setup_sizes(self):
        """Calculate the local sizes of all relevant variables
        and share those across all processes in the communicator.
        """
        # find all local systems
        sys_stack = [self._system]
        loc_comps = []

        while sys_stack:
            system = sys_stack.pop()
            loc_comps.extend([s.name for s in system.simple_subsystems()
                                    if s._comp is not None])
            sys_stack.extend(system.local_subsystems())

        loc_comps = set(loc_comps)
        loc_comps.add(None)

        # loop over all component inputs and boundary outputs and
        # set them to their sources so that they'll be sized properly
        for node, data in self._reduced_graph.nodes_iter(data=True):
            if 'comp' not in data:
                src = node[0]
                sval, idx = get_val_and_index(self, src)
                if isinstance(sval, ndarray):
                    dests = node[1]
                    for dest in dests:
                        dcomp = dest.split('.',1)[0] if '.' in dest else None
                        if dcomp in loc_comps:
                            dval, didx = get_val_and_index(self, dest)
                            if isinstance(dval, ndarray):
                                if sval.shape != dval.shape:
                                    self.set(dest, sval)

        # this will calculate sizes for all subsystems
        self._system.setup_sizes()

    def setup_vectors(self, arrays=None):
        """Creates vector wrapper objects to manage local and
        distributed vectors need to solve the distributed system.
        """
        self._system.setup_vectors(None)

    def setup_scatters(self):
        self._system.setup_scatters()

    def setup_depgraph(self, dgraph=None):
        # create the depgraph
        self._depgraph = DependencyGraph()
        
        #dgraph = self._depgraph.subgraph(self._depgraph.nodes_iter())
        # self._setup_depgraph = dgraph

        for name in self.list_inputs():
            self._depgraph.add_boundary_var(self, name,
                                            iotype='in')
            
        for name in self.list_outputs():
            self._depgraph.add_boundary_var(self, name,
                                            iotype='out')
        
        for comp in self.get_comps():
            self._depgraph.add_component(comp.name, comp)
     
        self._exprmapper.setup_depgraph(self, self._depgraph)
       
        precomps = self._check_unexecuted_comps(False)

        if precomps:
            ## HACK ALERT!
            ## If there are upstream comps that are not in any workflow,
            ## create a hidden top level driver called _pre_driver. That
            ## driver will be executed once per execution of the Assembly.
    
            # can't call add here for _pre_driver because that calls
            # config_changed...
            self._pre_driver = Driver()
            self._pre_driver.parent = self
            precomps.append('driver') # run the normal top driver after running the 'pre' comps
            self._pre_driver.workflow.add(precomps)
            self._pre_driver.name = '_pre_driver'
            self._depgraph.add_node('_pre_driver', comp=True, driver=True)

        for comp in self.get_comps():
            comp.setup_depgraph(self._depgraph)

    def setup_reduced_graph(self, inputs=None, outputs=None):
        """Create the graph we need to do the breakdown of the model
        into Systems.
        """
        dgraph = self._depgraph

        # keep all states
        # FIXME: I think this should only keep states of comps that are directly relevant...
        keep = set([n for n,d in dgraph.nodes_iter(data=True)
                         if d.get('iotype')=='state'])
                         #if d.get('iotype') in ('state','residual')])

        if self.parent is not None:
            self._derivs_required = self.parent._derivs_required
        else:
            self._derivs_required = False

        # figure out the relevant subgraph based on given inputs and outputs
        if not (inputs is None and outputs is None):
            self._derivs_required = True

        dsrcs, ddests = self._top_driver.get_expr_var_depends(recurse=True)
        keep.add(self._top_driver.name)
        keep.update([c.name for c in self._top_driver.iteration_set()])

        # keep any connected boundary vars
        for u,v in chain(dgraph.list_connections(), list_driver_connections(dgraph)):
            if is_boundary_node(dgraph, u):
                    keep.add(u)
            if is_boundary_node(dgraph, v):
                keep.add(v)

        if inputs is None:
            inputs = list(ddests)
        else:
            # fix any single tuples
            inputs = [fix_single_tuple(i) for i in inputs]

            # identify any broadcast inputs
            ins = []
            for inp in inputs:
                if isinstance(inp, basestring):
                    keep.add(inp)
                    ins.append(inp)
                else:
                    keep.update(inp)
                    ins.append(inp[0])
            inputs = ins

        if outputs is None:
            outputs = dsrcs

        dgraph = dgraph._explode_vartrees(self)

        # add any variables requested that don't exist in the graph
        for inp in inputs:
            if inp not in dgraph:
                base = base_var(dgraph, inp)
                for n in chain(dgraph.successors(base), dgraph.predecessors(base)):
                    if base_var(dgraph, n) != base:
                        keep.add(base)
                dgraph.add_connected_subvar(inp)

        for out in outputs:
            if out not in dgraph:
                base = base_var(dgraph, out)
                for n in chain(dgraph.successors(base), dgraph.predecessors(base)):
                    if base_var(dgraph, n) != base:
                        keep.add(base)
                dgraph.add_connected_subvar(out)

        dgraph = dgraph.relevant_subgraph(inputs, outputs, keep)

        dgraph._remove_vartrees(self)

        keep.update(inputs)
        keep.update(outputs)

        dgraph._fix_state_connections(self)

        dgraph._connect_subvars_to_comps()

        # collapse all connections into single nodes.
        collapsed_graph = dgraph.collapse_connections()
        collapsed_graph.fix_duplicate_dests()
        collapsed_graph.vars2tuples(dgraph)

        self.name2collapsed = collapsed_graph.map_collapsed_nodes()

        # add VarSystems for boundary vars
        for node, data in collapsed_graph.nodes_iter(data=True):
            if 'boundary' in data and collapsed_graph.degree(node) > 0:
                if data.get('iotype') == 'in' and collapsed_graph.in_degree(node) == 0: # input boundary node
                    collapsed_graph.add_node(node[0].split('[',1)[0], comp='invar')
                    collapsed_graph.add_edge(node[0].split('[',1)[0], node)
                elif data.get('iotype') == 'out': # output bndry node
                    collapsed_graph.add_node(node[1][0].split('[',1)[0], comp='outvar')
                    collapsed_graph.add_edge(node, node[1][0].split('[',1)[0])

        # translate kept nodes to collapsed form
        coll_keep = set([self.name2collapsed.get(k,k) for k in keep])

        # remove all vars that don't connect components
        collapsed_graph.prune(coll_keep)

        #rgraph = collapsed_graph.subgraph(collapsed_graph.nodes_iter())
        #rgraph.collapse_subdrivers([], [self._top_driver])
        #self._driver_collapsed_graph = rgraph

        collapsed_graph.fix_dangling_vars()

        self._reduced_graph = collapsed_graph

        for comp in self.get_comps():
            if has_interface(comp, IDriver) and comp.requires_derivs():
                self._derivs_required = True
            if has_interface(comp, IAssembly):
                comp.setup_reduced_graph(inputs=_get_scoped_inputs(comp, dgraph, inputs),
                                         outputs=_get_scoped_outputs(comp, dgraph, outputs))

    def _get_var_info(self, node):
        """Collect any variable metadata from the
        model here.
        """
        info = { 'size': 0 }

        base = None
        noflat = False

        if isinstance(node, tuple):
            # use the name of the src
            name = node[0]
            for n in simple_node_iter(node):
                if self.get_metadata(n.split('[',1)[0], 'noflat'):
                    noflat = True
                    break
        else:
            name = node

        parts = name.split('.',1)
        if len(parts) > 1:
            cname, vname = parts
            child = getattr(self, cname)
        else:
            cname, vname = '', name
            child = self

        try:
            # TODO: add checking of local_size metadata...
            val, idx = get_val_and_index(child, vname)

            if hasattr(val, 'shape'):
                info['shape'] = val.shape

            if '[' in vname:  # array index into basevar
                base = vname.split('[',1)[0]
                flat_idx = get_flattened_index(idx,
                                        get_var_shape(base, child),
                                        cvt_to_slice=False)
            else:
                base = None
                flat_idx = None

            info['size'] = flattened_size(vname, val, scope=child)
            if flat_idx is not None:
                info['flat_idx'] = flat_idx

        except NoFlatError:
            info['noflat'] = True

        if base is not None:
            if cname:
                bname = '.'.join((cname, base))
            else:
                bname = base
            info['basevar'] = bname

        # get any other metadata we want
        meta = child.get_metadata(vname)
        for mname in ['deriv_ignore']:
            if meta.get(mname) is not None:
                info[mname] = meta

        if noflat:
            info['noflat'] = True

        return info

    def _get_all_var_metadata(self, graph):
        """Collect size, shape, etc. info for all variables referenced
        in the graph.  This info can then be used by all subsystems
        contained in this Assembly.
        """
        varmeta = self._var_meta
        for node, data in graph.nodes_iter(data=True):
            if node not in varmeta and 'comp' not in data:
                meta = self._get_var_info(node)
                self._update_varmeta(node, meta)

        # there are cases where a component will return names from
        # its list_deriv_vars that are not found in the graph, so add them
        # all here just in case
        for node, data in graph.nodes_iter(data=True):
            if 'comp' in data and '.' not in node:
                try:
                    comp = getattr(self, node)
                    ins, outs = comp.list_deriv_vars()
                except AttributeError:
                    continue
                for name in chain(ins, outs):
                    name = '.'.join((node, name))
                    if name not in varmeta:
                        varmeta[name] = self._get_var_info(name)

    def _update_varmeta(self, node, meta):
        self._var_meta[node] = meta
        for name in simple_node_iter(node):
            self._var_meta[name] = meta

    def pre_setup(self):
        self._provideJ_bounds = None
        self.driver.pre_setup()

    def post_setup(self):
        for comp in self.get_comps():
            comp.post_setup()

        self._system.vec['u'].set_from_scope(self)

    def _setup(self, inputs=None, outputs=None):
        """This is called automatically on the top level Assembly
        prior to execution.  It will also be called if
        calc_gradient is called with input or output lists that
        differ from the lists of parameters or objectives/constraints
        that are inherent to the model.
        """

        if MPI:
            MPI.COMM_WORLD.Set_errhandler(MPI.ERRORS_ARE_FATAL)
            comm = MPI.COMM_WORLD
        else:
            comm = None

        self._var_meta = {}

        try:
            self.setup_depgraph()
            self.pre_setup()  # FIXME: change this name!
            self.setup_reduced_graph(inputs=inputs, outputs=outputs)
            self.check_config()            
            self.setup_systems()
            self.setup_communicators(comm)
            self.setup_variables()
            self.setup_sizes()
            self.setup_vectors()
            self.setup_scatters()
        except Exception:
            traceback.print_exc()
            raise
        self.post_setup()


def dump_iteration_tree(obj, f=sys.stdout, full=True, tabsize=4, derivs=False):
    """Returns a text version of the iteration tree
    of an OpenMDAO object.  The tree shows which are being
    iterated over by which drivers.

    If full is True, show pseudocomponents as well.
    If derivs is True, include derivative input/output
    information.
    """
    def _dump_iteration_tree(obj, f, tablevel):
        tab = ' ' * tablevel
        if is_instance(obj, Driver):
            f.write("%s%s\n" % (tab, obj.name))
            if derivs:
                raise NotImplementedError("dumping of derivative inputs/outputs not supported yet.")
                    # f.write("%s*deriv inputs: %s\n"
                    #         % (' '*(tablevel+tabsize+2), inputs))
                    # f.write("%s*deriv outputs: %s\n"
                    #         % (' '*(tablevel+tabsize+2), outputs))
            names = set(obj.workflow.get_names())
            for comp in obj.workflow:
                if not full and comp.name not in names:
                    continue
                if is_instance(comp, Driver) or is_instance(comp, Assembly):
                    _dump_iteration_tree(comp, f, tablevel + tabsize)
                elif is_instance(comp, PseudoComponent):
                    f.write("%s%s  (%s)\n" %
                        (' ' * (tablevel+tabsize), comp.name, comp._orig_expr))
                else:
                    f.write("%s%s\n" % (' ' * (tablevel+tabsize), comp.name))
        elif is_instance(obj, Assembly):
            f.write("%s%s\n" % (tab, obj.name))
            _dump_iteration_tree(obj.driver, f, tablevel + tabsize)

    _dump_iteration_tree(obj, f, 0)

def _get_scoped_inputs(comp, g, explicit_ins):
    """Return a list of input varnames scoped to the given name."""
    cnamedot = comp.name + '.'
    inputs = set()
    if explicit_ins is None:
        explicit_ins = ()

    for u,v in chain(g.list_connections(), list_driver_connections(g)):
        if v.startswith(cnamedot):
            inputs.add(v)

    inputs.update([n for n in explicit_ins if n.startswith(cnamedot)])

    if not inputs:
        return None

    return [n.split('.',1)[1] for n in inputs]

def _get_scoped_outputs(comp, g, explicit_outs):
    """Return a list of output varnames scoped to the given name."""
    cnamedot = comp.name + '.'
    outputs = set()
    if explicit_outs is None:
        explicit_outs = ()

    for u,v in chain(g.list_connections(), list_driver_connections(g)):
        if u.startswith(cnamedot):
            outputs.add(u)

    outputs.update([n for n in explicit_outs if n.startswith(cnamedot)])

    if not outputs:
        return None

    return [n.split('.',1)[1] for n in outputs]
