import torch
import torch.overrides
import linecache
from typing import Type, Dict, List, Any
from .graph import Graph
from copy import deepcopy

# normal exec loses the source code, however we can patch
# the linecache module to still recover it.
# using exec_with_source will add it to our local cache
# and then tools like TorchScript will be able to get source info.
_next_id = 0
def exec_with_source(src: str, globals: Dict[str, Any]):
    global _next_id
    key = f'<eval_with_key_{_next_id}>'
    _next_id += 1
    _eval_cache[key] = [line + '\n' for line in src.splitlines()]
    exec(compile(src, key, 'exec'), globals)

# patch linecache so that any code we exec using exec_with_source
# works with inspect
_eval_cache : Dict[str, List[str]] = {}
_orig_getlines = linecache.getlines
def patched_getline(*args, **kwargs):
    if args[0] in _eval_cache:
        return _eval_cache[args[0]]
    return _orig_getlines(*args, **kwargs)
linecache.getlines = patched_getline

def _forward_from_src(src : str):
    gbls: Dict[str, Any] = {
        'torch': torch
    }
    exec_with_source(src, gbls)
    return gbls['forward']


def deserialize_graphmodule(body : dict) -> torch.nn.Module:
    """
    Deserialize a GraphModule given the dictionary of the original module,
    using the code to reconstruct the graph. We delete the actual graph before
    saving the dictionary so that changes to the in-memory graph format do not
    get serialized.
    """
    # We create a dummy class here because symbolic_trace pulls the forward()
    # function off of the class, rather than the instance
    class CodeOnlyModule(torch.nn.Module):
        def __init__(self, body):
            super().__init__()
            self.__dict__ = body

    CodeOnlyModule.forward = _forward_from_src(body['code'])

    from .symbolic_trace import symbolic_trace, DefaultDelegate

    # we shouldn't trace into any of the submodules, they were not
    # because they were not traced in the original GraphModule
    class KeepModules(DefaultDelegate):
        def is_leaf_module(self, _: torch.nn.Module) -> bool:
            return True

    return symbolic_trace(CodeOnlyModule(body), delegate_class=KeepModules)

# copy an attribute value with qualified name 'target' from 'from_module' to 'to_module'
# This installs empty Modules where none exist yet if they are subpaths of target
def _copy_attr(from_module: torch.nn.Module, to_module: torch.nn.Module, target: str):
    *prefix, field = target.split('.')
    for item in prefix:
        f = getattr(from_module, item)
        t = getattr(to_module, item, None)
        if f is t:
            # we have already installed one of its parents
            # (e.g. target = root.linear.weight, but we have already installed root.linear)
            # once we install a parent, we no longer need to copy the children
            # since all the needed properties will already be present
            return

        if t is None:
            t = torch.nn.Module()
            setattr(to_module, item, t)
        from_module, to_module = f, t

    setattr(to_module, field, getattr(from_module, field))

class GraphModule(torch.nn.Module):
    def __new__(cls: 'Type[GraphModule]', *args, **kwargs):
        # each instance of a graph module needs its own forward method
        # so create a new singleton class for each instance.
        # it is a subclass of the user-defined class, the only difference
        # is an extra layer to install the forward method

        class GraphModuleImpl(cls):  # type: ignore
            pass
        return super().__new__(GraphModuleImpl)

    def __init__(self, root: torch.nn.Module, graph: Graph):
        super().__init__()
        if hasattr(root, 'training'):
            self.training = root.training
        for node in graph.nodes:
            if node.op in ['get_param', 'call_module']:
                assert isinstance(node.target, str)
                _copy_attr(root, self, node.target)
        self.graph = graph
        self._generate_forward()

    def _generate_forward(self) -> None:
        body, result, free_variables = self.graph.python_code(root_module='self')
        body = '\n'.join('    ' + line for line in body.split('\n')) + '\n'
        self.code = f"""\
def forward(self, {', '.join(free_variables)}):
{body}
    return {result}
"""
        cls = type(self)
        cls.forward = _forward_from_src(self.code)

    def __reduce__(self):
        dict_without_graph = self.__dict__.copy()
        del dict_without_graph['graph']
        return (deserialize_graphmodule, (dict_without_graph,))

    # because __reduce__ is defined for serialization,
    # we need to define deepcopy otherwise it will call __reduce__
    # and cause symbolic tracing to occur every time we try to copy the object
    def __deepcopy__(self, memo):
        the_copy = self.__new__(type(self))
        the_copy.__dict__ = deepcopy(self.__dict__, memo)
        return the_copy


    def __str__(self) -> str:
        orig_str = super().__str__()
        return '\n'.join([orig_str, self.code])

# workarounds for issues in __torch_function__

# WAR for __torch_function__ not handling tensor lists,
# fix is in https://github.com/pytorch/pytorch/pull/34725
# orig_cat = torch.cat
# def patched_cat(*args, **kwargs):
#     tensors = args[0]
#     for t in tensors:
#         if isinstance(t, Proxy):
#             return t.__torch_function__(patched_cat, (), args, kwargs)
#     return orig_cat(*args, **kwargs)
# patched_cat.__module__ = 'torch'
# patched_cat.__name__ = 'cat'
# torch.cat = patched_cat
