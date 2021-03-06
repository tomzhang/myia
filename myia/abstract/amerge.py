"""Implementation of the amerge utility."""

from contextvars import ContextVar
from functools import reduce
from itertools import chain

from .. import xtype
from ..utils import MyiaTypeError, TypeMismatchError, overload
from .data import (
    ABSENT,
    ANYTHING,
    AbstractBottom,
    AbstractClassBase,
    AbstractDict,
    AbstractError,
    AbstractFunction,
    AbstractScalar,
    AbstractTaggedUnion,
    AbstractTuple,
    AbstractUnion,
    AbstractWrapper,
    Possibilities,
    TaggedPossibilities,
    TrackDict,
    VirtualFunction,
)
from .loop import (
    Pending,
    PendingTentative,
    find_coherent_result_sync,
    is_simple,
)
from .utils import (
    CheckState,
    CloneState,
    abstract_check,
    broaden,
    is_broad,
    union_simplify,
)

amerge_engine = ContextVar("amerge_engine", default=None)


###################
# Tentative check #
###################


@is_broad.variant(initial_state=lambda: CheckState(cache={}, prop=None))
def _is_tentative(self, x: (Possibilities, TaggedPossibilities), loop):
    return False


#############
# Tentative #
#############


@broaden.variant(initial_state=lambda: CloneState({}, None, _is_tentative))
def tentative(self, p: Possibilities, loop):  # noqa: D417
    """Broaden an abstract value and make it tentative.

    * Concrete values such as 1 or True will be broadened to ANYTHING.
    * Possibilities will be broadened to PendingTentative. This allows
      us to avoid resolving them earlier than we would like.

    Arguments:
        p: The abstract data to clone.
        loop: The InferenceLoop, used to broaden Possibilities.

    """
    return loop.create_pending_tentative(tentative=p)


@overload  # noqa: F811
def tentative(self, p: TaggedPossibilities, loop):
    return loop.create_pending_tentative(tentative=p)


############
# Nobottom #
############


@abstract_check.variant
def nobottom(self, x: AbstractBottom):
    """Check whether bottom appears anywhere in this type."""
    return False


@overload  # noqa: F811
def nobottom(self, x: Pending, *args):
    return True


#########
# Merge #
#########


@overload.wrapper(bootstrap=True, initial_state=dict)
def amerge(
    __call__, self, x1, x2, forced=False, bind_pending=True, accept_pending=True
):
    """Merge two values.

    If forced is False, amerge will return a superset of x1 and x2, if it
    exists.

    If the forced argument is True, amerge will either return x1 or fail.
    This makes a difference in some situations:

        * amerge(1, 2, forced=False) => ANYTHING
        * amerge(1, 2, forced=True) => Error
        * amerge(ANYTHING, 1234, forced=True) => ANYTHING
        * amerge(1234, ANYTHING, forced=True) => Error

    Arguments:
        x1: The first value to merge
        x2: The second value to merge
        forced: Whether we are already committed to returning x1 or not.
        bind_pending: Whether we bind two Pending, unresolved values.
        accept_pending: Works the same as bind_pending, but only for the
            top level call.

    """
    if x1 is x2:
        return x1

    keypair = (id(x1), id(x2))
    if keypair in self.state:
        result = self.state[keypair]
        if result is ABSENT:
            # Setting forced=True will set the keypair to x1 (and then check
            # that x1 and x2 are compatible under forced=True), which lets us
            # return a result for self-referential data.
            return amerge(
                x1,
                x2,
                forced=True,
                bind_pending=bind_pending,
                accept_pending=accept_pending,
            )
        else:
            return result

    def helper():
        nonlocal x1, x2
        while isinstance(x1, Pending) and x1.done() and not forced:
            x1 = x1.result()
        while isinstance(x2, Pending) and x2.done():
            x2 = x2.result()
        isp1 = isinstance(x1, Pending)
        isp2 = isinstance(x2, Pending)
        loop = x1.get_loop() if isp1 else x2.get_loop() if isp2 else None
        if isinstance(x1, PendingTentative):
            new_tentative = self(x1.tentative, x2, False, True, bind_pending)
            assert not isinstance(new_tentative, Pending)
            x1.tentative = new_tentative
            return x1
        if isinstance(x2, PendingTentative):
            new_tentative = self(
                x1, x2.tentative, forced, bind_pending, accept_pending
            )
            assert not isinstance(new_tentative, Pending)
            x2.tentative = new_tentative
            return new_tentative if forced else x2
        if (isp1 or isp2) and (not accept_pending or not bind_pending):
            if forced and isp1:
                raise MyiaTypeError("Cannot have Pending here.")
            if isp1:

                def chk(a):
                    return self(a, x2, forced, bind_pending)

                return find_coherent_result_sync(x1, chk)
            if isp2:

                def chk(a):
                    return self(x1, a, forced, bind_pending)

                return find_coherent_result_sync(x2, chk)
        if isp1 and isp2:
            return bind(loop, x1 if forced else None, [], [x1, x2])
        elif isp1:
            return bind(loop, x1 if forced else None, [x2], [x1])
        elif isp2:
            return bind(loop, x1 if forced else None, [x1], [x2])
        elif isinstance(x2, AbstractBottom):  # pragma: no cover
            return x1
        elif isinstance(x1, AbstractBottom):
            if forced:  # pragma: no cover
                # I am not sure how to trigger this
                raise TypeMismatchError(x1, x2)
            return x2
        elif x1 is ANYTHING:
            return x1
        elif x2 is ANYTHING:
            if forced:
                raise TypeMismatchError(x1, x2)
            return x2
        elif type(x1) is not type(x2) and not isinstance(
            x1, (int, float, bool)
        ):
            raise MyiaTypeError(
                f"Type mismatch: {type(x1)} != {type(x2)}; {x1} != {x2}"
            )
        else:
            return self.map[type(x1)](self, x1, x2, forced, bind_pending)

    self.state[keypair] = x1 if forced else ABSENT
    rval = helper()
    self.state[keypair] = rval
    if forced:
        assert rval is x1
    return rval


@overload  # noqa: F811
def amerge(self, x1: Possibilities, x2, forced, bp):
    eng = amerge_engine.get()
    poss = x1 + x2
    if all(isinstance(x, VirtualFunction) for x in poss):
        assert not forced
        return Possibilities(
            [
                VirtualFunction(
                    reduce(self, [x.args for x in poss]),
                    reduce(self, [x.output for x in poss]),
                )
            ]
        )

    for standard in poss:
        # TODO: This is a hack of sorts until we replace Possibilities
        # inside AbstractFunction by AbstractUnion, and AbsFunc only has
        # one function inside it.
        if isinstance(standard, VirtualFunction):
            for entry in poss:
                if not isinstance(entry, VirtualFunction):
                    eng.loop.schedule(
                        eng.infer_function(
                            entry, standard.args, standard.output
                        )
                    )
            break

    if set(x1).issuperset(set(x2)):
        return x1
    if forced:
        raise MyiaTypeError("Additional Possibilities cannot be merged.")
    else:
        return Possibilities(x1 + x2)


@overload  # noqa: F811
def amerge(self, x1: TaggedPossibilities, x2, forced, bp):
    d1 = dict(x1)
    d2 = dict(x2)
    results = {}
    for i, t in d1.items():
        if i in d2:
            t = self(t, d2[i], forced, bp)
        results[i] = t
    for i, t in d2.items():
        if i not in d1:
            results[i] = t
    res = TaggedPossibilities(results.items())
    if res == x1:
        return x1
    elif forced:
        raise MyiaTypeError("Additional TaggedPossibilities cannot be merged.")
    elif res == x2:
        return x2
    else:
        return res


@overload  # noqa: F811
def amerge(self, x1: xtype.TypeMeta, x2, forced, bp):
    if issubclass(x2, x1):
        return x1
    elif not forced and issubclass(x1, x2):
        return x2
    else:
        raise TypeMismatchError(x1, x2)


@overload  # noqa: F811
def amerge(self, x1: TrackDict, x2, forced, bp):
    keys = {*x1.keys(), *x2.keys()}
    rval = type(x1)()
    changes = False
    for k in keys:
        if k in x1:
            v1 = x1[k]
        else:
            v1 = k.default()
            changes = True
        v2 = x2[k] if k in x2 else k.default()
        res = k.merge(self, v1, v2, forced, bp)
        if res is not v1:
            changes = True
        if res is not ABSENT:
            rval[k] = res
    if forced and changes and rval != x1:
        raise MyiaTypeError("Cannot merge tracks")
    return x1 if forced or not changes else rval


@overload  # noqa: F811
def amerge(self, x1: dict, x2, forced, bp):
    if set(x1.keys()) != set(x2.keys()):
        raise MyiaTypeError(f"Keys mismatch")
    changes = False
    rval = type(x1)()
    for k, v in x1.items():
        res = self(v, x2[k], forced, bp)
        if res is not v:
            changes = True
        rval[k] = res
    return x1 if forced or not changes else rval


@overload  # noqa: F811
def amerge(self, x1: (tuple, list), x2, forced, bp):
    if len(x1) != len(x2):  # pragma: no cover
        raise MyiaTypeError(f"Tuple length mismatch")
    changes = False
    rval = []
    for v1, v2 in zip(x1, x2):
        res = self(v1, v2, forced, bp)
        if res is not v1:
            changes = True
        rval.append(res)
    return x1 if forced or not changes else type(x1)(rval)


@overload  # noqa: F811
def amerge(self, x1: AbstractScalar, x2, forced, bp):
    values = self(x1.values, x2.values, forced, bp)
    if forced or values is x1.values:
        return x1
    return AbstractScalar(values)


@overload  # noqa: F811
def amerge(self, x1: AbstractError, x2, forced, bp):
    e1 = x1.xvalue()
    e2 = x2.xvalue()
    e = self(e1, e2, forced, bp)
    if forced or e is e1:
        return x1
    return AbstractError(e)


@overload  # noqa: F811
def amerge(self, x1: AbstractFunction, x2, forced, bp):
    values = self(x1.get_sync(), x2.get_sync(), forced, bp)
    if forced or values is x1.values:
        return x1
    return AbstractFunction(*values)


@overload  # noqa: F811
def amerge(self, x1: AbstractTuple, x2, forced, bp):
    args1 = (x1.elements, x1.values)
    args2 = (x2.elements, x2.values)
    merged = self(args1, args2, forced, bp)
    if forced or merged is args1:
        return x1
    return AbstractTuple(*merged)


@overload  # noqa: F811
def amerge(self, x1: AbstractWrapper, x2, forced, bp):
    args1 = (x1.element, x1.values)
    args2 = (x2.element, x2.values)
    merged = self(args1, args2, forced, bp)
    if forced or merged is args1:
        return x1
    return type(x1)(*merged)


@overload  # noqa: F811
def amerge(self, x1: AbstractClassBase, x2, forced, bp):
    args1 = (x1.tag, x1.attributes, x1.values)
    args2 = (x2.tag, x2.attributes, x2.values)
    merged = self(args1, args2, forced, bp)
    if forced or merged is args1:
        return x1
    tag, attrs, values = merged
    return type(x1)(tag, attrs, values=values)


@overload  # noqa: F811
def amerge(self, x1: AbstractDict, x2, forced, bp):
    args1 = (x1.entries, x1.values)
    args2 = (x2.entries, x2.values)
    merged = self(args1, args2, forced, bp)
    if forced or merged is args1:
        return x1
    return type(x1)(*merged)


@overload  # noqa: F811
def amerge(self, x1: (AbstractUnion, AbstractTaggedUnion), x2, forced, bp):
    args1 = x1.options
    args2 = x2.options
    merged = self(args1, args2, forced, bp)
    if forced or merged is args1:
        return x1
    return type(x1)(merged)


@overload  # noqa: F811
def amerge(self, x1: (int, float, bool), x2, forced, bp):
    if forced and x1 != x2:
        raise TypeMismatchError(x1, x2)
    return x1 if x1 == x2 else ANYTHING


@overload  # noqa: F811
def amerge(self, x1: object, x2, forced, bp):
    if x1 != x2:
        raise TypeMismatchError(x1, x2)
    return x1


def bind(loop, committed, resolved, pending):
    """Bind Pendings together.

    Arguments:
        loop: The InferenceLoop.
        committed: Either None, or an abstract value that we are already
            committed to, which will force the merge to return that value.
        resolved: A set of Pendings that have already been resolved.
        pending: A set of unresolved Pendings.

    """

    def amergeall():
        if committed is None:
            v = reduce(
                lambda x1, x2: amerge(
                    x1, x2, forced=False, accept_pending=False
                ),
                resolved,
            )
        else:
            v = reduce(
                lambda x1, x2: amerge(
                    x1, x2, forced=True, accept_pending=False
                ),
                resolved,
                committed,
            )
        return v

    resolved = list(resolved)
    pending = set(pending)
    assert pending

    def resolve(fut):
        nonlocal committed
        pending.remove(fut)
        result = fut.result()
        if fut is committed:
            committed = result
        resolved.append(result)
        if not pending:
            v = amergeall()
            if merged is not None and not merged.done():
                merged.set_result(v)

    for p in pending:
        p.add_done_callback(resolve)

    def premature_resolve():
        # This is what force_resolve() on the result will do
        nonlocal committed
        # We merge what we have so far
        committed = amergeall()
        # We broaden the result so that the as-of-yet unresolved stuff
        # can be merged more easily.
        committed = tentative(committed, loop)
        resolved.clear()
        return committed

    def priority():
        # Cannot force resolve unless we have at least one resolved Pending
        if not resolved and committed is None:
            return None
        if any(is_simple(x) for x in chain([committed], resolved, pending)):
            return 1000
        elif any(not nobottom(x) for x in resolved):
            # Bottom is always lower-priority
            return None
        else:
            return -1000

    if any(is_simple(x) for x in chain(resolved, pending)):
        # merged = None because we will not make a new Pending
        merged = None

        if pending:
            p, *rest = pending
            p.equiv.update(resolved)
            for p2 in rest:
                p.tie(p2)

        if resolved:
            rval = resolved[0]
        else:
            for p in pending:
                if is_simple(p):
                    rval = p
                    break
            else:
                raise AssertionError("unreachable")

    else:
        merged = loop.create_pending(
            resolve=premature_resolve, priority=priority
        )
        merged.equiv.update(resolved)
        for p in pending:
            merged.tie(p)
        rval = merged

    if committed is None:
        return rval
    else:
        return committed


###########################
# Typing-related routines #
###########################


# def collapse_options(options):
#     """Collapse a list of options, some of which may be AbstractUnions."""
#     opts = []
#     todo = list(options)
#     while todo:
#         option = todo.pop()
#         if isinstance(option, AbstractUnion):
#             todo.extend(option.options)
#         else:
#             opts.append(option)
#     opts = Possibilities(opts)
#     return opts


# def union_simplify(options, constructor=AbstractUnion):
#     """Simplify a list of options.

#     Returns:
#         * None, if there are no options.
#         * A single type, if there is one option.
#         * An AbstractUnion.

#     """
#     options = collapse_options(options)
#     if len(options) == 0:
#         return None
#     elif len(options) == 1:
#         return options.pop()
#     else:
#         return constructor(options)


def split_type(t, model):
    """Checks t against the model and return matching/non-matching subtypes.

    * If t is a Union, return a Union that fully matches model, and a Union
      that does not match model. No matches in either case returns None for
      that case.
    * Otherwise, return (t, None) or (None, t) depending on whether t matches
      the model.
    """
    if isinstance(t, AbstractUnion):
        matching = [(opt, typecheck(model, opt)) for opt in set(t.options)]
        t1 = union_simplify(opt for opt, m in matching if m)
        t2 = union_simplify(opt for opt, m in matching if not m)
        return t1, t2
    elif typecheck(model, t):
        return t, None
    else:
        return None, t


def hastype_helper(value, model):
    """Helper to implement hastype."""
    if isinstance(model, AbstractUnion):
        results = [hastype_helper(value, opt) for opt in model.options]
        if any(r is True for r in results):
            return True
        elif all(r is False for r in results):
            return False
        else:
            return ANYTHING
    else:
        match, nomatch = split_type(value, model)
        if match is None:
            return False
        elif nomatch is None:
            return True
        else:
            return ANYTHING


def typecheck(model, abstract):
    """Check that abstract matches the model."""
    try:
        amerge(model, abstract, forced=True, bind_pending=False)
    except MyiaTypeError:
        return False
    else:
        return True


__all__ = [
    "amerge",
    "bind",
    "hastype_helper",
    "nobottom",
    "split_type",
    "typecheck",
]
