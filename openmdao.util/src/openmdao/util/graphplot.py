import os
import sys
import time
import shutil
import json
import tempfile
import webbrowser
import pprint

from networkx.readwrite.json_graph import node_link_data

_excluded_node_data = set([
    'pa_object',
])

_excluded_link_data = set([
    'sexpr',
    'dexpr',
])

_excluded_tooltip_data = set([
    'short',
    'comp',
    'var',
    'invalidation',
    'boundary',
    'iotype',
    'color_idx',
    'title',
    'pseudo',
])

def _to_id(name):
    """Convert a given name to a valid html id, replacing
    dots with hyphens."""
    return name.replace('.', '-')

def _clean_graph(graph, excludes=(), scope=None, parent=None):
    """Return a cleaned version of the graph. Note that this
    should not be used for really large graphs because it 
    copies the entire graph.
    """
    # make a subgraph, creating new edge/node meta dicts later if
    # we change anything
    graph = graph.subgraph(graph.nodes_iter())

    if parent is None:
        graph.graph['title'] = 'unknown'
    else:
        name = parent.get_pathname()
        if hasattr(parent, 'workflow'):
            name += '._derivative_graph'
        else:
            name += '._depgraph'
        graph.graph['title'] = name

    if not excludes:
        from openmdao.main.component import Component
        from openmdao.main.driver import Driver
        excluded_vars = set()

        if scope:
            try:
                cgraph = graph.component_graph()
            except AttributeError:
                pass
            else:
                for cname in cgraph.nodes():
                    try:
                        comp = getattr(scope, cname)
                    except AttributeError:
                        pass
                    else:
                        if isinstance(comp, Component):
                            graph.remove_nodes_from(['.'.join([cname,n]) 
                                    for n,v in comp.items(framework_var=True)])

        else: # just exclude some framework vars from Driver and Component
            c = Component()
            d = Driver()
            excluded_vars = set([n for n,v in 
                                           c.items(framework_var=True)])
            excluded_vars.update([n for n,v in 
                                           d.items(framework_var=True)])
    else:
        excluded_vars = set(excludes)

    conns = graph.list_connections()

    conn_nodes = set([u.split('[',1)[0] for u,v in conns])
    conn_nodes.update([v.split('[',1)[0] for u,v in conns])

    nodes_to_remove = []
    for node, data in graph.nodes_iter(data=True):
        cmpname, _, nodvar = node.partition('.')
        if node in excluded_vars or nodvar in excluded_vars:
            nodes_to_remove.append(node)
        else: # update node metadata
            newdata = data
            for meta in _excluded_node_data:
                if meta in newdata:
                    if newdata is data:
                        newdata = dict(data) # make a copy of metadata since we're changing it
                        graph.node[node] = newdata
                    del newdata[meta]
            tt_dct = {}
            for key,val in newdata.items():
                if key not in _excluded_tooltip_data:
                    tt_dct[key] = val
                elif scope is not None and key == 'pseudo':
                    if val == 'objective':
                        newdata['objective'] = getattr(scope, node)._orig_src
                    elif val == 'constraint':
                        newdata['constraint'] = getattr(scope, node)._orig_src
            newdata['title'] = pprint.pformat(tt_dct)

    graph.remove_nodes_from(nodes_to_remove)

    for u,v,data in graph.edges_iter(data=True):
        newdata = data
        for meta in _excluded_link_data:
            if meta in newdata:
                if newdata is data:
                    newdata = dict(data)
                    graph.edge[u][v] = newdata
                del newdata[meta]

    try:
        for i,comp in enumerate(graph.component_graph()):
            graph.node[comp]['color_idx'] = i
    except AttributeError:
        pass

    # add some extra metadata to make things easier on the
    # javascript side
    for node, data in graph.nodes_iter(data=True):
        parts = node.split('.', 1)
        if len(parts) == 1:
            data['short'] = node
        else:
            data['short'] = parts[1]
            try:
                data['color_idx'] = graph.node[parts[0]]['color_idx']
            except KeyError:
                pass

    return graph

def plot_graph(graph, scope=None, parent=None, 
               excludes=(), d3page='fixedforce.html'):
    """Open up a display of the graph in a browser window."""

    tmpdir = tempfile.mkdtemp()
    fdir = os.path.dirname(os.path.abspath(__file__))
    shutil.copy(os.path.join(fdir, 'd3.js'), tmpdir)
    shutil.copy(os.path.join(fdir, d3page), tmpdir)

    graph = _clean_graph(graph, excludes=excludes, 
                         scope=scope, parent=parent)
    data = node_link_data(graph)
    tmp = data.get('graph', [])
    data['graph'] = [dict(tmp)]

    startdir = os.getcwd()
    os.chdir(tmpdir)
    try:
        # write out the json as a javascript var
        # so we we're not forced to start our own webserver
        # to avoid cross-site issues
        with open('__graph.js', 'w') as f:
            f.write("__mygraph__json = ")
            json.dump(data, f)
            f.write(";\n")

        # open URL in web browser
        wb = webbrowser.get()
        wb.open('file://'+os.path.join(tmpdir, d3page))
    finally:
        os.chdir(startdir)
        print "\nwaiting to remove temp directory '%s'... " % tmpdir
        time.sleep(5) # sleep to give browser time
                       # to read files before we remove them
        shutil.rmtree(tmpdir)
        print "temp directory removed"

def plot_graphs(obj, recurse=False):
    """Return a list of tuples of the form (scope, parent, graph)"""
    from openmdao.main.assembly import Assembly
    from openmdao.main.driver import Driver

    if isinstance(obj, Assembly):
        plot_graph(obj._depgraph, scope=obj, parent=obj)
        if recurse:
            plot_graphs(obj.driver, recurse)
    elif isinstance(obj, Driver):
        plot_graph(obj.workflow.derivative_graph(), 
                   scope=obj.parent, parent=obj)
        if recurse:
            for comp in obj.iteration_set():
                if isinstance(comp, Assembly) or isinstance(comp, Driver):
                    plot_graphs(comp, recurse)

def main():
    from argparse import ArgumentParser
    import inspect

    from openmdao.main.assembly import Assembly, set_as_top

    parser = ArgumentParser()
    parser.add_argument('-m', '--module', action='store', dest='module',
                        metavar='MODULE',
                        help='name of module that contains the class to be instantiated and graphed')
    parser.add_argument('-c', '--class', action='store', dest='klass',
                        help='boolean expression to filter hosts')
    parser.add_argument('-r', '--recurse', action='store_true', dest='recurse',
                        help='if set, recurse down and plot all dependency and derivative graphs')

    options = parser.parse_args()
    
    if options.module is None:
        parser.print_help()
        sys.exit(-1)
               
    __import__(options.module)

    mod = sys.modules[options.module]

    if options.klass:
        obj = getattr(mod, options.klass)()
    else:
        def isasm(obj):
            return issubclass(obj, Assembly)

        klasses = inspect.getmembers(mod, isasm)
        if len(klasses) > 1:
            print "found %d Assembly classes.  pick one" % len(klasses)
            for i, klass in enumerate(klasses):
                print "%d) %s" % (i, klass.__name__)
            var = raw_input("\nEnter a number: ")
            obj = klasses[int(var)]

    set_as_top(obj)
    if not obj.get_pathname():
        obj.name = 'top'

    plot_graphs(obj, recurse=options.recurse)

if __name__ == '__main__':
    main()




