"""Value fingerprints and the post-import walk shared by snap.py (selector) and keymap.py (map build)."""
import enum, functools, sys, types


def fp(v, seen=()):
    if type(v) in (type(None), bool, int, float, complex, str, bytes):
        return (type(v).__name__, v)
    if id(v) in seen:
        return ("cyc",)
    seen = seen + (id(v),)
    if isinstance(v, enum.Enum):
        return ("enum", type(v).__qualname__, v.name)
    if isinstance(v, type):
        return ("cls", v.__module__, v.__qualname__)
    if isinstance(v, types.ModuleType):
        return ("mod", v.__name__)
    if isinstance(v, (tuple, list)):
        return (type(v).__name__,) + tuple(fp(x, seen) for x in v)
    if isinstance(v, (set, frozenset)):
        return (type(v).__name__,) + tuple(sorted((fp(x, seen) for x in v), key=repr))
    if isinstance(v, dict):
        return ("dict",) + tuple(sorted(((fp(k, seen), fp(x, seen)) for k, x in dict.items(v)), key=repr))
    if isinstance(v, types.CodeType):
        return ("code", v.co_code, v.co_names, v.co_varnames, v.co_freevars, tuple(fp(c, seen) for c in v.co_consts))
    if isinstance(v, types.FunctionType):
        cells = []
        for c in v.__closure__ or ():
            try:
                cells.append(fp(c.cell_contents, seen))
            except ValueError:  # empty cell
                cells.append(None)
        return ("fn", v.__module__, v.__qualname__, fp(v.__code__, seen), fp(v.__defaults__, seen),
                fp(v.__kwdefaults__, seen), tuple(cells))
    if isinstance(v, (staticmethod, classmethod)):
        return (type(v).__name__, fp(v.__func__, seen))
    if isinstance(v, property):
        return ("prop", fp(v.fget, seen), fp(v.fset, seen))
    if isinstance(v, functools.partial):
        return ("partial", fp(v.func, seen), fp(v.args, seen), fp(v.keywords, seen))
    if isinstance(v, (types.BuiltinFunctionType, types.MethodDescriptorType, types.WrapperDescriptorType)):
        return ("builtin", getattr(v, "__qualname__", repr(v)))
    if isinstance(v, types.MethodType):
        return ("meth", fp(v.__func__, seen), fp(v.__self__, seen))
    d = getattr(v, "__dict__", None)
    if isinstance(d, dict):
        return ("obj", type(v).__module__, type(v).__qualname__, fp(d, seen))
    slots = [s for k in type(v).__mro__ for s in getattr(k, "__slots__", ()) if isinstance(s, str)]
    if slots:
        return ("slots", type(v).__qualname__) + tuple(fp(getattr(v, s, None), seen) for s in slots)
    r = repr(v)
    return ("repr", type(v).__qualname__, r if " at 0x" not in r else "?")  # ponytail: an address can't be compared


def kfp(k):
    return repr(fp(k))


def walk(each):
    """each(label, value) for every global of every loaded sqlglot module and every attribute of its classes."""

    done, later = set(), []

    def cls(c, label):
        done.add(c)
        for a, v in list(vars(c).items()):
            if a in ("__dict__", "__weakref__", "__doc__"):
                continue
            if isinstance(v, type) and v.__module__ == c.__module__ and v.__qualname__.startswith(c.__qualname__ + "."):
                cls(v, label + "." + a)
            else:
                if isinstance(v, type) and (v.__module__ or "").startswith("sqlglot"):
                    later.append((label + "." + a, v))  # e.g. a Tokenizer subclass a metaclass made with type()
                each(label + "." + a, v)

    for name, mod in sorted(sys.modules.items()):
        if not name.startswith("sqlglot") or mod is None:
            continue
        for g, v in list(vars(mod).items()):
            if g.startswith("__") and g.endswith("__"):
                continue
            if isinstance(v, type) and v.__module__ == name:
                cls(v, name + "." + v.__qualname__)
            else:
                each(name + "." + g, v)
    for label, v in later:  # grows while it runs
        if v not in done:
            cls(v, label)
