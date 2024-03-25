import dataclasses
import functools
import inspect
import sys
import typing
import weakref

from torchgen.model import FunctionSchema, OperatorName, SchemaKind, BaseType, ListType, BaseTy

import torch
import torch._C as _C
import torch.library as library
from torch._library.abstract_impl import AbstractImplCtx
from torch.library import get_ctx

from .autograd import autograd_kernel_indirection, construct_autograd_kernel

"""
For a detailed guide on custom ops, please see
https://docs.google.com/document/d/1aGWtgxV3HppuxQAdddyPrs74_aEntpkYt9MalnCKnhk

This file includes pieces of the implementation of our custom operator API.
"""

__all__ = ["custom_op", "CustomOp", "get_ctx", "AbstractImplCtx"]


SUPPORTED_DEVICE_TYPE_TO_KEY = {
    "cpu": "CPU",
    "cuda": "CUDA",
}

# We will not let users register CustomOps with anything that could look like
# PyTorch internals to avoid confusion.
RESERVED_NS = {
    "prim",
    "prims",
    "aten",
    "at",
    "torch",
    "pytorch",
}


def custom_op(
    qualname: str, manual_schema: typing.Optional[str] = None
) -> typing.Callable:
    r"""Creates a new CustomOp object.

    WARNING: if you're a user, please do not use this directly
    (instead use the torch._custom_ops APIs).
    Also please see the following for a detailed guide on custom ops.
    https://docs.google.com/document/d/1aGWtgxV3HppuxQAdddyPrs74_aEntpkYt9MalnCKnhk

    In PyTorch, defining an op (short for "operator") is a two step-process:
    - we need to define (create) the op
    - we need to implement behavior for how the operator interacts with
      various PyTorch subsystems, like CPU/CUDA Tensors, Autograd, etc.

    This entrypoint defines the CustomOp object (the first step);
    you must then perform the second step by calling various methods on
    the CustomOp object.

    This API is used as a decorator (see examples).

    Arguments:
        qualname (str): Should be a string that looks like
            "namespace::operator_name". Operators in PyTorch need a namespace to
            avoid name collisions; a given operator may only be created once.
            If you are writing a Python library, we recommend the namespace to
            be the name of your top-level module. The operator_name must be
            the same as the name of the function you pass to custom_op
            (see examples).
        manual_schema (Optional[str]): Each PyTorch operator needs a schema that
            tells PyTorch the types of the inputs/outputs. If None (default),
            we will infer the schema from the type annotations on the function
            (see examples). Otherwise, if you don't want to use type annotations,
            you may provide us the schema string.

    Example::
        >>> # xdoctest: +REQUIRES(env:TORCH_DOCTEST_CUDA)
        >>> import numpy as np
        >>> from torch import Tensor
        >>>
        >>> # Step 1: define the CustomOp.
        >>> # We need to provide the decorator a "prototype function"
        >>> # (a function with Python ellipses as the body).
        >>> @custom_op("my_library::numpy_sin")
        >>> def numpy_sin(x: Tensor) -> Tensor:
        >>>     ...
        >>>
        >>> # numpy_sin is now an instance of class CustomOp
        >>> print(type(numpy_sin))
        >>>
        >>> # Step 2: Register an implementation for various PyTorch subsystems
        >>>
        >>> # Register an implementation for CPU tensors
        >>> @numpy_sin.impl('cpu')
        >>> def numpy_sin_impl_cpu(x):
        >>>     return torch.from_numpy(np.sin(x.numpy()))
        >>>
        >>> # Register an implementation for CUDA tensors
        >>> @numpy_sin.impl('cuda')
        >>> def numpy_sin_impl_cuda(x):
        >>>     return torch.from_numpy(np.sin(x.cpu().numpy())).to(x.device)
        >>>
        >>> x = torch.randn(3)
        >>> numpy_sin(x)  # calls numpy_sin_impl_cpu
        >>>
        >>> x_cuda = x.cuda()
        >>> numpy_sin(x)  # calls numpy_sin_impl_cuda

    """

    def inner(func):
        if not inspect.isfunction(func):
            raise ValueError(
                f"custom_op(...)(func): Expected `func` to be a Python "
                f"function, got: {type(func)}"
            )

        ns, name = parse_qualname(qualname)
        validate_namespace(ns)
        if func.__name__ != name:
            raise ValueError(
                f"custom_op(qualname='{qualname}', ...)(func): expected `func` "
                f"to have name '{name}' but got '{func.__name__}'. "
                f"Please either change the name of `func` or the qualname that "
                f"is passed to `custom_op`"
            )

        schema = infer_schema(func) if manual_schema is None else manual_schema
        schema_str = f"{name}{schema}"
        function_schema = FunctionSchema.parse(schema_str)
        validate_schema(function_schema)
        if manual_schema is not None:
            validate_function_matches_schema(function_schema, func)

        lib = library.Library(ns, "FRAGMENT")
        lib.define(schema_str)
        ophandle = find_ophandle_or_throw(ns, function_schema.name)
        result = CustomOp(lib, ns, function_schema, name, ophandle, _private_access=True)

        result.__name__ = func.__name__
        result.__module__ = func.__module__
        result.__doc__ = func.__doc__

        library.impl(lib, result._opname, "Autograd")(
            autograd_kernel_indirection(weakref.proxy(result))
        )

        torch._C._dispatch_set_report_error_callback(
            ophandle, functools.partial(report_error_callback, weakref.proxy(result))
        )

        return result

    return inner


# Global dictionary holding references to all CustomOp objects
# Yes, it keeps all CustomOps alive (see NOTE [CustomOp lifetime])
# Used to query the CustomOp associated with a specific C++ dispatcher operator.
# An example usage is FakeTensor: FakeTensor checks if a specific operator
# has an implementation registered via the CustomOp API.
# Indexed by qualname (e.g. aten::foo)
global_registry: typing.Dict[str, "CustomOp"] = {}


class CustomOp:
    r"""Class for custom operators in PyTorch.

    Use the CustomOp API to create user-defined custom operators that behave
    just like regular PyTorch operators (e.g. torch.sin, torch.mm) when it
    comes to various PyTorch subsystems (like torch.compile).

    To construct a `CustomOp`, use `custom_op`.
    """

    def __init__(self, lib, cpp_ns, schema, operator_name, ophandle, *, _private_access=False):
        super().__init__()
        if not _private_access:
            raise RuntimeError(
                "The CustomOp constructor is private and we do not guarantee "
                "BC for it. Please use custom_op(...) to create a CustomOp object"
            )
        name = f"{cpp_ns}::{operator_name}"
        self._schema = schema
        self._cpp_ns = cpp_ns
        self._lib: library.Library = lib
        self._ophandle: _C._DispatchOperatorHandle = ophandle
        # Has the name of the op, e.g. "foo". We cache here for convenience.
        self._opname: str = operator_name
        # this is _opname but with namespace. e.g. "custom::foo"
        self._qualname: str = name
        self.__name__ = None  # mypy requires this
        # NB: Some of these impls are registered as kernels to DispatchKeys.
        # Modifying the _impls dict directly won't do anything in that case.
        self._impls: typing.Dict[str, typing.Optional[FuncAndLocation]] = {}
        # See NOTE [CustomOp autograd kernel indirection]
        self._registered_autograd_kernel_indirection = False

        global_registry[self._qualname] = self

    def _register_autograd_kernel_indirection(self):
        assert not self._registered_autograd_kernel_indirection
        self._lib.impl(self._opname, autograd_kernel_indirection(weakref.proxy(self)), "Autograd")
        self._registered_autograd_kernel_indirection = True

    # Records the impl and the source location in self._impls
    # Note that this doesn't cause torch.library to use the impl, that
    # needs to be done in a separate self._lib.impl call.
    def _register_impl(self, kind, func, stacklevel=2):
        if self._has_impl(kind):
            func_and_location = self._impls[kind]
            assert func_and_location is not None  # Pacify mypy
            location = func_and_location.location
            raise RuntimeError(
                f"Attempting to register a {kind} impl for operator {self._qualname} "
                f"that already has a {kind} impl registered from Python at "
                f"{location}. This is not supported."
            )
        frame = inspect.getframeinfo(sys._getframe(stacklevel))
        location = f"{frame.filename}:{frame.lineno}"
        self._impls[kind] = FuncAndLocation(func, location)

    def _get_impl(self, kind):
        return self._impls[kind]

    def _has_impl(self, kind):
        return kind in self._impls

    def _destroy(self):
        # NOTE: [CustomOp lifetime]
        # A CustomOp, once created, lives forever. The mechanism is that the
        # global registry holds a reference to it. However, to make testing
        # easier, we want to be able to destroy CustomOp objects.
        # CustomOp._destroy does the job, though it leaves the CustomOp
        # in a garbage state.
        del self._lib

        opnamespace = getattr(torch.ops, self._cpp_ns)
        if hasattr(opnamespace, self._opname):
            delattr(opnamespace, self._opname)

        del global_registry[self._qualname]

    def __repr__(self):
        return f'<CustomOp(op="{self._qualname}")>'

    def __call__(self, *args, **kwargs):
        # Bypass torch.ops.* and directly do OperatorHandle::callBoxed.
        # Using torch.ops.* is a bit of a pain (it can be slow and it has lifetime
        # issues from caching operators that make testing CustomOp difficult).
        result = _C._dispatch_call_boxed(self._ophandle, *args, **kwargs)
        return result

    def impl(
        self, device_types: typing.Union[str, typing.Iterable[str]], _stacklevel=2,
    ) -> typing.Callable:
        r"""Register an implementation for a device type for this CustomOp object.

        WARNING: if you're a user, please do not use this directly
        (instead use the torch._custom_ops APIs).
        Also please see the following for a detailed guide on custom ops.
        https://docs.google.com/document/d/1aGWtgxV3HppuxQAdddyPrs74_aEntpkYt9MalnCKnhk

        If the CustomOp is passed multiple Tensor inputs with different device
        types, it will dispatch to the registered implementation for the highest
        priority device type among those present.
        The supported device types, in order of priority, are {'cuda', 'cpu'}.

        This API is used as a decorator (see examples).

        Arguments:
            device_types (str or Iterable[str]): the device type(s) to register the function for.

        Examples::
            >>> # xdoctest: +REQUIRES(env:TORCH_DOCTEST_CUDA)
            >>> import numpy as np
            >>> from torch import Tensor
            >>>
            >>> @custom_op("my_library::numpy_cos")
            >>> def numpy_cos(x: Tensor) -> Tensor:
            >>>     ...
            >>>
            >>> # Register an implementation for CPU Tensors
            >>> @numpy_cos.impl('cpu')
            >>> def numpy_cos_impl_cpu(x):
            >>>     return torch.from_numpy(np.cos(x.numpy()))
            >>>
            >>> # Register an implementation for CUDA Tensors
            >>> @numpy_cos.impl('cuda')
            >>> def numpy_cos_impl_cuda(x):
            >>>     return torch.from_numpy(np.cos(x.cpu().numpy())).to(x.device)
            >>>
            >>> x = torch.randn(3)
            >>> numpy_cos(x)  # calls numpy_cos_impl_cpu
            >>>
            >>> x_cuda = x.cuda()
            >>> numpy_cos(x)  # calls numpy_cos_impl_cuda

        """
        if isinstance(device_types, str):
            device_types = [device_types]
        for device_type in device_types:
            validate_device_type(device_type)

        def inner(f):
            for device_type in set(device_types):
                self._check_doesnt_have_library_impl(device_type)
                self._register_impl(device_type, f, stacklevel=_stacklevel)
                dispatch_key = SUPPORTED_DEVICE_TYPE_TO_KEY[device_type]
                library.impl(self._lib, self._opname, dispatch_key)(f)
            return f

        return inner

    def _check_doesnt_have_library_impl(self, device_type):
        if self._has_impl(device_type):
            return
        key = SUPPORTED_DEVICE_TYPE_TO_KEY[device_type]
        if _C._dispatch_has_computed_kernel_for_dispatch_key(self._qualname, key):
            raise RuntimeError(
                f"impl(..., device_types={device_type}): the operator {self._qualname} "
                f"already has an implementation for this device type via a "
                f"pre-existing torch.library or TORCH_LIBRARY registration.")

    def impl_factory(self) -> typing.Callable:
        r"""Register an implementation for a factory function."""

        def inner(f):
            self._register_impl("factory", f)
            library.impl(self._lib, self._opname, "BackendSelect")(f)
            return f

        return inner

    def impl_abstract(self, _stacklevel=2) -> typing.Callable:
        r"""Register an abstract implementation for this operator.

        WARNING: please do not use this directly (and instead use the torch._custom_ops
        APIs). Also please see the following for a detailed guide on custom ops.
        https://docs.google.com/document/d/1aGWtgxV3HppuxQAdddyPrs74_aEntpkYt9MalnCKnhk

        An "abstract implementation" specifies the behavior of this operator on
        Tensors that carry no data. Given some input Tensors with certain properties
        (sizes/strides/storage_offset/device), it specifies what the properties of
        the output Tensors are.

        The abstract implementation has the same signature as the operator.
        It is run for both FakeTensors and meta tensors. To write an abstract
        implementation, assume that all Tensor inputs to the operator are
        regular CPU/CUDA/Meta tensors, but they do not have storage, and
        you are trying to return regular CPU/CUDA/Meta tensor(s) as output.
        The abstract implementation must consist of only PyTorch operations
        (and may not directly access the storage or data of any input or
        intermediate Tensors).

        This API is used as a decorator (see examples).

        Examples::
            >>> import numpy as np
            >>> from torch import Tensor
            >>>
            >>> # Example 1: an operator without data-dependent output shape
            >>> @custom_op('my_library::custom_linear')
            >>> def custom_linear(x: Tensor, weight: Tensor, bias: Tensor) -> Tensor:
            >>>     ...
            >>>
            >>> @custom_linear.impl_abstract()
            >>> def custom_linear_abstract(x, weight):
            >>>     assert x.dim() == 2
            >>>     assert weight.dim() == 2
            >>>     assert bias.dim() == 1
            >>>     assert x.shape[1] == weight.shape[1]
            >>>     assert weight.shape[0] == bias.shape[0]
            >>>     assert x.device == weight.device
            >>>
            >>>     return (x @ weight.t()) + bias
            >>>
            >>> # Example 2: an operator with data-dependent output shape
            >>> @custom_op('my_library::custom_nonzero')
            >>> def custom_nonzero(x: Tensor) -> Tensor:
            >>>     ...
            >>>
            >>> @custom_nonzero.impl_abstract()
            >>> def custom_nonzero_abstract(x):
            >>>     # Number of nonzero-elements is data-dependent.
            >>>     # Since we cannot peek at the data in an abstract impl,
            >>>     # we use the ctx object to construct a new symint that
            >>>     # represents the data-dependent size.
            >>>     ctx = torch._custom_op.get_ctx()
            >>>     nnz = ctx.create_unbacked_symint()
            >>>     shape = [x.dim(), nnz]
            >>>     result = x.new_empty(shape, dtype=torch.long)
            >>>     return result
            >>>
            >>> @custom_nonzero.impl(['cpu', 'cuda'])
            >>> def custom_nonzero_impl(x):
            >>>     x_np = to_numpy(x)
            >>>     res = np.stack(np.nonzero(x_np), axis=1)
            >>>     # unbacked symbolic ints in PyTorch must be >= 2, so we
            >>>     # constrain the range to at least 2
            >>>     if res.shape[0] <= 1:
            >>>         raise RuntimeError("not supported")
            >>>     return torch.tensor(res, device=x.device)

        """

        def inner(f):
            self._check_doesnt_have_library_meta_impl()
            self._register_impl("abstract", f, stacklevel=_stacklevel)
            location = self._get_impl("abstract").location

            qualname = self._qualname

            # Handle DispatchKey.Meta registration
            @functools.wraps(f)
            def f_with_ctx(*args, **kwargs):
                def error_on_ctx():
                    raise RuntimeError(
                        f"Attempted to call get_ctx() for the meta implementation "
                        f"for {qualname}."
                        f"You have presumably called get_ctx() because the operator "
                        f"has a data-dependent output shape; if so, there is no "
                        f"such meta implementation and this error is the correct "
                        f"behavior. Otherwise, please remove the call to get_ctx() "
                        f"in the implementation registered with impl_abstract "
                        f"at {location}"
                    )

                with torch._library.abstract_impl.set_ctx_getter(error_on_ctx):
                    return f(*args, **kwargs)

            self._lib.impl(self._opname, f_with_ctx, "Meta")
            return f

        return inner

    def _check_can_register_backward(self):
        def error(detail):
            raise RuntimeError(
                f"Cannot use torch._custom_ops APIs to register backward "
                f"formula for {detail}. Got operator "
                f"{self._qualname} with schema: {schema}"
            )

        schema = self._schema
        if schema.kind() != SchemaKind.functional:
            error("non-functional operator")

        rets = schema.returns
        if not schema.returns:
            error("operator with no returns")

        assert len(rets) > 0
        is_non_mutating_view = any(
            r.annotation is not None and not r.annotation.is_write for r in rets
        )
        if is_non_mutating_view:
            error("operator that returns views")

        # We make assumptions about the schema's return types.
        allowed_return_types = {
            BaseType(BaseTy.int): "int",
            BaseType(BaseTy.SymInt): "SymInt",
            BaseType(BaseTy.bool): "bool",
            BaseType(BaseTy.float): "float",
            BaseType(BaseTy.Tensor): "Tensor",
            ListType(BaseType(BaseTy.Tensor), None): "List[Tensor]",
        }
        for ret in schema.returns:
            if ret.type in allowed_return_types:
                continue
            error(f"operator with return not in {list(allowed_return_types.values())} (got {ret.type})")

    def _check_doesnt_have_library_autograd_impl(self):
        if self._registered_autograd_kernel_indirection:
            return

        if _C._dispatch_has_kernel_for_dispatch_key(self._qualname, "CompositeImplicitAutograd"):
            raise RuntimeError(
                f"impl_backward/impl_save_for_backward: the operator {self._qualname} "
                f"already has an implementation for this device type via a "
                f"pre-existing registration to DispatchKey::CompositeImplicitAutograd."
                f"CompositeImplicitAutograd operators do not need an autograd formula; "
                f"instead, the operator will decompose into its constituents and those "
                f"can have autograd formulas defined on them.")

        # We can improve this by adding "all Autograd<BACKEND> keys", but
        # realistically people will just be using this API for CPU/CUDA for now.
        for key in ["Autograd", "AutogradCPU", "AutogradCUDA"]:
            if _C._dispatch_has_kernel_for_dispatch_key(self._qualname, key):
                raise RuntimeError(
                    f"impl_backward/impl_save_for_backward: "
                    f"the operator {self._qualname} already has an Autograd kernel "
                    f"registered to DispatchKey::{key} vi a pre-existing "
                    f"torch.library or TORCH_LIBRARY registration. Please either "
                    f"remove those registrations or don't use the torch._custom_ops APIs")

    def _check_doesnt_have_library_meta_impl(self):
        if self._has_impl("abstract"):
            return

        # If the user's operator is CompositeExplicitAutograd,
        # allow them to impl_abstract. This is being pragmatic
        # (existing custom ops may have CompositeExplicitAutograd
        # registration that don't work with Meta kernels, so this
        # gives them an escape hatch).
        if (
            _C._dispatch_has_kernel_for_dispatch_key(self._qualname, "CompositeExplicitAutograd")
            and not _C._dispatch_has_kernel_for_dispatch_key(self._qualname, "Meta")
        ):
            return

        # Otherwise, if the user's already has a Meta kernel or their
        # op is CompositeImplicitAutograd or some other alias dispatch key,
        # raise.

        # Special case for CompositeImplicitAutograd
        if _C._dispatch_has_kernel_for_dispatch_key(self._qualname, "CompositeImplicitAutograd"):
            raise RuntimeError(
                f"impl_abstract(...): the operator {self._qualname} "
                f"already has an implementation for this device type via a "
                f"pre-existing registration to DispatchKey::CompositeImplicitAutograd."
                f"CompositeImplicitAutograd operators do not need an abstract impl; "
                f"instead, the operator will decompose into its constituents and those "
                f"can have abstract impls defined on them.")

        if _C._dispatch_has_kernel_for_dispatch_key(self._qualname, "Meta"):
            raise RuntimeError(
                f"impl_abstract(...): the operator {self._qualname} "
                f"already has an DispatchKey::Meta implementation via a "
                f"pre-existing torch.library or TORCH_LIBRARY registration. "
                f"Please either remove that registration or don't call impl_abstract.")

    # NOTE ["backward", "save_for_backward", and "autograd"]
    # As a part of the explicit autograd API, a user must provide us
    # a "save_for_backward" function and a "backward" function.
    # When both of these have been provided, then we automatically
    # construct the "autograd" kernel.
    def _register_autograd_kernel(self):
        assert self._has_impl("backward")
        assert self._has_impl("save_for_backward")
        kernel = construct_autograd_kernel(
            self._schema,
            self._output_differentiability,
            self,
            get_op(self._qualname),
            self._get_impl("save_for_backward").func,
            self._get_impl("backward").func)
        self._register_impl("autograd", kernel)

    def impl_save_for_backward(self, _stacklevel=2):
        r"""Register a function that tells us what to save for backward.

        Please see impl_backward for more details.
        """
        def inner(f):
            self._check_can_register_backward()
            self._check_doesnt_have_library_autograd_impl()
            if not self._registered_autograd_kernel_indirection:
                self._register_autograd_kernel_indirection()
            self._register_impl("save_for_backward", f, stacklevel=_stacklevel)
            if self._has_impl("backward"):
                self._register_autograd_kernel()
        return inner

    def impl_backward(self, output_differentiability=None, _stacklevel=2):
        r"""Registers a backward formula.

        WARNING: if you're a user, please do not use this directly
        (instead use the torch._custom_ops APIs).
        Also please see the following for a detailed guide on custom ops.
        https://docs.google.com/document/d/1aGWtgxV3HppuxQAdddyPrs74_aEntpkYt9MalnCKnhk

        In order for the CustomOp to work with autograd, you need to register
        a backward formula. There are two pieces to this:
        1. You must give us a function to specify what to save for backward.
           Call this the "save for backward" function.
        2. You must give us a function that computes gradients. Call this the
           "backward" function.

        Use `impl_save_for_backward` to define a "save for backward" function
        that specifies what gets saved for backward. The function should accept
        two arguments ``(inputs, output)`` and return the quantities to be saved
        for backward.

        During runtime, when you call the CustomOp, PyTorch will invoke the
        "save for backward" function with the inputs and output of the CustomOp.

        Use `impl_backward` to define the "backward" function. The backward
        function must accept ``(ctx, saved, *grads)``:
        - ``ctx`` is a context object where we may provide information
        - ``saved`` is exactly what gets returned from the "save for backward"
          function
        - ``grads`` is one or more gradients. The number of gradients matches
          the number of outputs of the CustomOp.

        The backward function must return a dict that maps the name of
        an input to the CustomOp to its corresponding gradient. All inputs that
        were declared to be Tensors in the CustomOp definition must be accounted
        for in the dict. The gradient may be a Tensor or None.

        """
        if output_differentiability is not None:
            def yell():
                raise RuntimeError(
                    f"impl_backward(output_differentiability): expected "
                    f"output_differentiability to be a list of bools with "
                    f"length equal to the number of outputs of this CustomOp "
                    f"got: {output_differentiability}")

            if not isinstance(output_differentiability, list):
                yell()
            for diff in output_differentiability:
                if not isinstance(diff, bool):
                    yell()
            if len(self._schema.returns) != len(output_differentiability):
                yell()

        def inner(f):
            self._check_can_register_backward()
            self._check_doesnt_have_library_autograd_impl()
            if not self._registered_autograd_kernel_indirection:
                self._register_autograd_kernel_indirection()
            self._register_impl("backward", f, stacklevel=_stacklevel)
            self._output_differentiability = output_differentiability
            if self._has_impl("save_for_backward"):
                self._register_autograd_kernel()
        return inner


@dataclasses.dataclass
class FuncAndLocation:
    func: typing.Callable
    location: str


def find_ophandle_or_throw(cpp_ns: str, operator_name: OperatorName):
    overload_name = (
        "" if operator_name.overload_name is None else operator_name.overload_name
    )
    return _C._dispatch_find_schema_or_throw(
        f"{cpp_ns}::{str(operator_name.name)}", overload_name
    )


def validate_namespace(ns: str) -> None:
    if "." in ns:
        raise ValueError(
            f'custom_op(..., ns="{ns}"): expected ns to not contain any . (and be a '
            f"valid variable name)"
        )
    if ns in RESERVED_NS:
        raise ValueError(
            f"custom_op(..., ns='{ns}'): '{ns}' is a reserved namespace, "
            f"please choose something else. "
        )

def validate_schema(schema: FunctionSchema) -> None:
    if not torch._library.utils.is_functional_schema(schema):
        raise ValueError(
            f"custom_op only supports functional operators "
            f"(ops that do not mutate any inputs, do not return "
            f"views of the inputs, and has at least one return). "
            f"Got the following non-functional schema: {schema}"
        )

    # For simplicity: don't allow self arguments
    if schema.arguments.self_arg is not None:
        raise ValueError(
            f"custom_op does not support arguments named 'self'. Please "
            f"rename your argument. Got: {schema}"
        )


def parse_qualname(qualname: str) -> typing.Tuple[str, str]:
    names = qualname.split("::", 1)
    if len(names) != 2:
        raise ValueError(f"Expected there to be a namespace in {qualname}, i.e. The "
                         f"operator name should look something like ns::foo")
    if '.' in names[1]:
        raise ValueError(f"The torch.custom_ops APIs do not handle overloads, "
                         f"i.e. operator names with '.' in them. "
                         f"Please name your operator something like ns::foo. "
                         f"Got: {qualname}")
    return names[0], names[1]


def validate_device_type(device_type: str) -> None:
    if device_type not in SUPPORTED_DEVICE_TYPE_TO_KEY:
        raise ValueError(
            f"CustomOp.impl(device_types=[{device_type}, ...]): we only support device_type "
            f"in {SUPPORTED_DEVICE_TYPE_TO_KEY.keys()}."
        )


def supported_param(param: inspect.Parameter) -> bool:
    return param.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


def validate_function_matches_schema(
    schema: FunctionSchema, func: typing.Callable
) -> None:
    sig = inspect.signature(func)

    if not all(supported_param(p) for _, p in sig.parameters.items()):
        raise ValueError(
            f"custom_op(..., manual_schema)(func): positional-only args, "
            f"varargs, and kwargs are not supported. Please rewrite `func` "
            f"to not have them. Got `func` with signature: {sig}"
        )

    if (
        any(
            p.annotation is not inspect.Parameter.empty
            for _, p in sig.parameters.items()
        )
        or sig.return_annotation is not inspect.Signature.empty
    ):
        raise ValueError(
            f"custom_op(..., manual_schema)(func): When passing in a manual "
            f"schema, we expect `func` to have no type annotations to avoid "
            f"ambiguity. Got `func` with signature: {sig}"
        )

    positional = [
        (name, param)
        for name, param in sig.parameters.items()
        if param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
    ]
    kwargonly = [
        (name, param)
        for name, param in sig.parameters.items()
        if param.kind == inspect.Parameter.KEYWORD_ONLY
    ]

    def error():
        raise ValueError(
            f"custom_op(..., manual_schema)(func): When passing in a manual "
            f"schema, we expect `func`'s signature to match `manual_schema` "
            f"(aside from type annotations). "
            f"func's signature: {sig}, manual_schema: {schema}"
        )

    def error_default_args():
        raise ValueError(
            f"custom_op(..., manual_schema)(func): "
            f"neither func nor manual_schema should have default "
            f"arguments. Got "
            f"func's signature: {sig}, manual_schema: {schema}"
        )

    def compare(sig_args, schema_args):
        if len(sig_args) != len(schema_args):
            error()
        for (name, param), arg in zip(sig_args, schema_args):
            if name != arg.name:
                error()
            if param.default is not inspect.Parameter.empty or arg.default is not None:
                error_default_args()

    compare(positional, schema.arguments.flat_positional)
    compare(kwargonly, schema.arguments.flat_kwarg_only)


def infer_schema(prototype_function: typing.Callable, mutated_args=()) -> str:
    """Given a function with type hints, parses a schema.

    We make some assumptions to make our lives easier that correspond to how people
    write custom ops in real life:
    - none of the outputs alias any of the inputs or each other.
    - only the args listed in mutated_args are being mutated.

    Callers (e.g. the custom ops API) are responsible for checking these assumptions.
    """
    sig = inspect.signature(prototype_function)

    def error_fn(what):
        raise ValueError(
            f"infer_schema(func): {what} " f"Got func with signature {sig})"
        )

    params = []
    for idx, (name, param) in enumerate(sig.parameters.items()):
        if not supported_param(param):
            error_fn("We do not support positional-only args, varargs, or varkwargs.")

        if param.annotation is inspect.Parameter.empty:
            error_fn(f"Parameter {name} must have a type annotation.")

        if param.annotation not in SUPPORTED_PARAM_TYPES.keys():
            error_fn(
                f"Parameter {name} has unsupported type {param.annotation}. "
                f"The valid types are: {SUPPORTED_PARAM_TYPES.keys()}."
            )

        if param.default is not inspect.Parameter.empty:
            error_fn(
                f"Parameter {name} has a default value; this is not supported. "
                f"If you want to use default values then create a function with "
                f"default values that invokes the custom op."
            )
        schema_type = SUPPORTED_PARAM_TYPES[param.annotation]
        if name in mutated_args:
            if not schema_type.startswith("Tensor"):
                error_fn(f"Parameter {name} is in mutable_args but only Tensors or collections of Tensors can be mutated")
            schema_type = f"Tensor(a{idx}!){schema_type[len('Tensor'):]}"
        params.append(f"{schema_type} {name}")
    ret = parse_return(sig.return_annotation, error_fn)
    return f"({', '.join(params)}) -> {ret}"


def derived_types(
    base_type, cpp_type, list_base, optional_base_list, optional_list_base
):
    result = [
        (base_type, cpp_type),
        (typing.Optional[base_type], f"{cpp_type}?"),
    ]

    def derived_seq_types(typ):
        return [
            typing.Sequence[typ],  # type: ignore[valid-type]
            typing.List[typ],  # type: ignore[valid-type]
        ]

    if list_base:
        for seq_typ in derived_seq_types(base_type):
            result.append((seq_typ, f"{cpp_type}[]"))  # type: ignore[valid-type]
    if optional_base_list:
        for seq_typ in derived_seq_types(typing.Optional[base_type]):
            result.append((seq_typ, f"{cpp_type}?[]"))  # type: ignore[valid-type]
    if optional_list_base:
        for seq_typ in derived_seq_types(base_type):  # type: ignore[valid-type]
            result.append((typing.Optional[seq_typ], f"{cpp_type}[]?"))  # type: ignore[valid-type]
    return result


def get_supported_param_types():
    data = [
        # (python type, schema type, type[] variant, type?[] variant, type[]? variant
        (torch.Tensor, "Tensor", True, True, False),
        (int, "SymInt", True, False, True),
        (float, "float", True, False, True),
        (bool, "bool", True, False, True),
        (str, "str", False, False, False),
        (torch.types.Number, "Scalar", True, False, False),
        (torch.dtype, "ScalarType", False, False, False),
        (torch.device, "Device", False, False, False),
    ]
    result = []
    for line in data:
        result.extend(derived_types(*line))
    return dict(result)


SUPPORTED_RETURN_TYPES = {
    torch.Tensor: "Tensor",
    typing.List[torch.Tensor]: "Tensor[]",
    int: "SymInt",
    float: "float",
    bool: "bool",
    torch.types.Number: "Scalar",
}


def parse_return(annotation, error_fn):
    if annotation is None:
        return "()"

    origin = typing.get_origin(annotation)
    if origin is not tuple:
        if annotation not in SUPPORTED_RETURN_TYPES.keys():
            error_fn(
                f"Return has unsupported type {annotation}. "
                f"The valid types are: {SUPPORTED_RETURN_TYPES}."
            )
        return SUPPORTED_RETURN_TYPES[annotation]

    args = typing.get_args(annotation)
    for arg in args:
        if arg not in SUPPORTED_RETURN_TYPES:
            error_fn(
                f"Return has unsupported type {annotation}. "
                f"The valid types are: {SUPPORTED_RETURN_TYPES}."
            )

    return "(" + ", ".join([SUPPORTED_RETURN_TYPES[arg] for arg in args]) + ")"


SUPPORTED_PARAM_TYPES = get_supported_param_types()


def report_error_callback(custom_op: typing.Any, key: str) -> None:
    if key == "Undefined":
        raise NotImplementedError(
            f"{custom_op}: There were no Tensor inputs to this operator "
            f"(e.g. you passed an empty list of Tensors). If your operator is a "
            f"factory function (that is, it takes no Tensors and constructs "
            f"a new one), then please use CustomOp.impl_factory to register "
            f"an implementation for it"
        )
    if key == "Meta":
        raise NotImplementedError(
            f"{custom_op}: when running with device='Meta' tensors: there is no "
            f"abstract impl registered for this CustomOp. Please register one via "
            f"CustomOp.impl_abstract to get this CustomOp to work with Meta tensors"
        )
    if key in ("CPU", "CUDA"):
        device = key.lower()
        raise NotImplementedError(
            f"{custom_op}: when running with device='{device}' tensors: there is no "
            f"{device} impl registered for this CustomOp. Please register one via "
            f"CustomOp.impl(device_type='{device}')"
        )
    raise NotImplementedError(
        f"{custom_op}: No implementation for dispatch key {key}. It is likely "
        f"that we have not added this functionality yet, please either open an "
        f"issue or if you're feeling adventurous, use the low-level "
        f"torch.library API"
    )


def custom_op_from_existing(op):
    ns = op.namespace
    lib = torch.library.Library(ns, "FRAGMENT")
    name = op.name().split("::")[-1]
    schema_str = str(op._schema)
    # CustomOp expects the schema string without the namespace
    schema_str = schema_str.split("::")[-1]
    schema = FunctionSchema.parse(schema_str)
    return CustomOp(lib, ns, schema, name, op, _private_access=True)


def get_op(qualname):
    def error_not_found():
        raise ValueError(
            f"Could not find the operator {qualname}. Please make sure you have "
            f"already registered the operator and (if registered from C++) "
            f"loaded it via torch.ops.load_library.")

    ns, name = parse_qualname(qualname)
    if not hasattr(torch.ops, ns):
        error_not_found()
    opnamespace = getattr(torch.ops, ns)
    if not hasattr(opnamespace, name):
        error_not_found()
    packet = getattr(opnamespace, name)
    if not hasattr(packet, 'default'):
        error_not_found()
    return packet.default


def _find_custom_op(qualname, also_check_torch_library=False):
    if qualname in global_registry:
        return global_registry[qualname]
    if not also_check_torch_library:
        raise RuntimeError(
            f"Could not find custom op \"{qualname}\". Did you register it via "
            f"the torch._custom_ops API?")
    overload = get_op(qualname)
    result = custom_op_from_existing(overload)
    return result


def get_abstract_impl(qualname):
    if qualname not in torch._custom_op.impl.global_registry:
        return None
    custom_op = torch._custom_op.impl.global_registry[qualname]
    if custom_op is None:
        return None
    if not custom_op._has_impl("abstract"):
        return None
    return custom_op._get_impl("abstract").func


def _custom_op_with_schema(qualname, schema, needs_fixed_stride_order=True):
    ns, name = qualname.split("::")
    schema_str = f"{name}{schema}"
    function_schema = FunctionSchema.parse(schema_str)
    validate_schema(function_schema)
    tags = [torch._C.Tag.needs_fixed_stride_order] if needs_fixed_stride_order else []
    lib = library.Library(ns, "FRAGMENT")
    lib.define(schema_str, tags=tags)
    ophandle = find_ophandle_or_throw(ns, function_schema.name)
    result = CustomOp(lib, ns, function_schema, name, ophandle, _private_access=True)
    result._register_autograd_kernel_indirection()

    torch._C._dispatch_set_report_error_callback(
        ophandle, functools.partial(report_error_callback, weakref.proxy(result))
    )
    return get_op(qualname)
