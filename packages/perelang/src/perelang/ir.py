"""Lowers the type-checked surface AST (`ast_nodes.py`) into a distinct intermediate
representation whose every expression node carries its *resolved* `types.Type` — codegen
(`bytecode.py`, `llvm_backend.py`) never re-infers or guesses a value's type, it just reads `.ty`.

Three things make this genuinely distinct from the AST, not a renamed copy:

1. **Every expression node is annotated with a resolved `Type`.** The surface AST has none — see
   `ast_nodes.py`'s own docstring on why (`ast_nodes.Expr` is what was *written*; `types.Type` is
   what was *inferred*). Getting per-node types requires re-running Algorithm W's traversal at
   lowering time (`types.infer_program` only returns each *top-level* name's final type, not a
   type per subexpression) — `_Lowerer` below mirrors `types.Inferencer`'s private `_infer_*`
   methods structurally, using only its public surface (`unify`, `apply`, `compose`, `TypeEnv`,
   `TypeScheme`, `generalize`, `Inferencer.fresh/instantiate/type_ann_to_type`), and additionally
   builds an IR node at every step instead of discarding it. A node's `ty` may still contain
   unresolved type variables the moment it's built (later unification elsewhere in the same
   declaration can still refine it) — `_resolve_ir_expr` does one final substitution pass over
   each declaration's freshly-built IR, once its full substitution is known, to fix that up.

2. **Name resolution happens once, here.** `IrName.is_global` distinguishes a top-level `fn`/`let`
   (resolved by name in the VM's globals table) from a local/parameter/capture (resolved by stack
   slot at compile time) — so `bytecode.py` never re-derives "is this a global or a local" from
   string comparisons against a live symbol table.

3. **Closures are made explicit.** `ast.Lambda` is just `params` + `body`; `IrClosure` additionally
   carries `captures` — the ordered names of outer-scope bindings the body actually reads — computed
   once by free-variable analysis (`_free_local_names`) so `bytecode.py`/`vm.py` don't have to.
   Every capture is by value: this language has no reassignment (`=` only ever introduces a new
   `let` binding, never mutates one — see `ast_nodes.LetDecl`), so capture-by-value and
   capture-by-reference are observationally identical, and by-value is the simpler runtime.

`if` is deliberately *not* desugared into basic blocks here — `IrIf` keeps the surface's two-block
shape. Both this IR and `bytecode.py`'s compiler treat every expression as leaving exactly one
value on evaluation (a block with no tail leaves `Unit`), so `if`/`else` compiles to a jump over a
jump with no separate CFG data structure needed; basic-block lowering would be pure overhead for a
tree-walking bytecode compiler and a JIT-per-function LLVM backend, neither of which does CFG-level
optimization.
"""

from __future__ import annotations

from dataclasses import dataclass

from perelang import ast_nodes as ast
from perelang.errors import Span, TypeCheckError
from perelang.types import (
    T_BOOL,
    T_FLOAT,
    T_INT,
    T_STRING,
    T_UNIT,
    Inferencer,
    Substitution,
    TFun,
    Type,
    TypeEnv,
    TypeScheme,
    apply,
    apply_env,
    compose,
    generalize,
    t_list,
    unify,
)

# ---- IR expression nodes ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IrInt:
    value: int
    ty: Type
    span: Span


@dataclass(frozen=True, slots=True)
class IrFloat:
    value: float
    ty: Type
    span: Span


@dataclass(frozen=True, slots=True)
class IrStr:
    value: str
    ty: Type
    span: Span


@dataclass(frozen=True, slots=True)
class IrBool:
    value: bool
    ty: Type
    span: Span


@dataclass(frozen=True, slots=True)
class IrName:
    """A resolved reference to a binding. `is_global=True` means `bytecode.py` compiles this to a
    `LOAD_GLOBAL` (looked up by name in the VM's global table at run time); `False` means a
    `LOAD_LOCAL`/`LOAD_CAPTURE` (resolved to a fixed stack slot or closure-capture index at compile
    time). Which one a name is was already decided lexically during lowering (`_Lowerer._is_global`),
    so this field is just recording that decision, not deferring it."""

    name: str
    is_global: bool
    ty: Type
    span: Span


@dataclass(frozen=True, slots=True)
class IrUnary:
    op: str
    operand: IrExpr
    ty: Type
    span: Span


@dataclass(frozen=True, slots=True)
class IrBinary:
    op: str
    left: IrExpr
    right: IrExpr
    ty: Type
    span: Span


@dataclass(frozen=True, slots=True)
class IrIf:
    cond: IrExpr
    then_branch: IrBlock
    else_branch: IrBlock
    ty: Type
    span: Span


@dataclass(frozen=True, slots=True)
class IrCall:
    callee: IrExpr
    args: tuple[IrExpr, ...]
    ty: Type
    span: Span


@dataclass(frozen=True, slots=True)
class IrListLit:
    items: tuple[IrExpr, ...]
    ty: Type
    span: Span


@dataclass(frozen=True, slots=True)
class IrClosure:
    """A lowered lambda. `captures` is the ordered list of free-variable names the body reads from
    outside its own parameter list (see the module docstring, point 3, and `_free_local_names`
    below) — empty for a lambda that captures nothing, in which case it behaves exactly like a
    plain top-level function at run time (see `vm.py`'s `Closure`, which represents both)."""

    params: tuple[str, ...]
    param_types: tuple[Type, ...]
    body: IrExpr
    captures: tuple[str, ...]
    ty: Type
    span: Span


@dataclass(frozen=True, slots=True)
class IrLet:
    name: str
    value: IrExpr
    span: Span


@dataclass(frozen=True, slots=True)
class IrExprStmt:
    expr: IrExpr
    span: Span


IrStmt = IrLet | IrExprStmt


@dataclass(frozen=True, slots=True)
class IrBlock:
    stmts: tuple[IrStmt, ...]
    tail: IrExpr | None
    ty: Type
    span: Span


IrExpr = (
    IrInt | IrFloat | IrStr | IrBool | IrName | IrUnary | IrBinary | IrIf | IrCall | IrListLit | IrClosure | IrBlock
)


# ---- IR top-level declarations ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IrFunction:
    name: str
    params: tuple[str, ...]
    param_types: tuple[Type, ...]
    ret_type: Type
    body: IrBlock
    span: Span


@dataclass(frozen=True, slots=True)
class IrGlobalLet:
    name: str
    value: IrExpr
    ty: Type
    span: Span


IrDecl = IrFunction | IrGlobalLet


@dataclass(frozen=True, slots=True)
class IrProgram:
    decls: tuple[IrDecl, ...]


# ---- free-variable analysis for closure captures ------------------------------------------------


def _free_local_names(node: IrExpr, bound: frozenset[str]) -> set[str]:
    """Every non-global name `node` reads that isn't in `bound` — used once per `IrClosure` (see
    `_Lowerer._lower_lambda`) with `bound` set to that lambda's own parameter names, over its
    already-lowered body, to compute exactly what it needs to capture.

    A nested `IrClosure`'s own body is *not* re-walked here — its `captures` field already *is*
    its free names relative to its enclosing scope (this function computed it, recursively, when
    that inner lambda was lowered), so re-deriving them would be redundant; we just union them in,
    still subject to the same `bound` filter (a name the inner closure captures might be one of
    *this* lambda's own parameters, in which case it's not free here either)."""
    match node:
        case IrInt() | IrFloat() | IrStr() | IrBool():
            return set()
        case IrName(name, is_global, _ty):
            return set() if is_global or name in bound else {name}
        case IrUnary(_op, operand, _ty):
            return _free_local_names(operand, bound)
        case IrBinary(_op, left, right, _ty):
            return _free_local_names(left, bound) | _free_local_names(right, bound)
        case IrIf(cond, then_b, else_b, _ty):
            return _free_local_names(cond, bound) | _free_block(then_b, bound) | _free_block(else_b, bound)
        case IrCall(callee, args, _ty):
            result = _free_local_names(callee, bound)
            for a in args:
                result |= _free_local_names(a, bound)
            return result
        case IrListLit(items, _ty):
            result = set()
            for item in items:
                result |= _free_local_names(item, bound)
            return result
        case IrClosure(_params, _pt, _body, captures, _ty):
            return {c for c in captures if c not in bound}
        case IrBlock():
            return _free_block(node, bound)


def _free_block(block: IrBlock, bound: frozenset[str]) -> set[str]:
    result: set[str] = set()
    cur_bound = bound
    for stmt in block.stmts:
        match stmt:
            case IrLet(name, value):
                result |= _free_local_names(value, cur_bound)
                cur_bound = cur_bound | {name}
            case IrExprStmt(expr):
                result |= _free_local_names(expr, cur_bound)
    if block.tail is not None:
        result |= _free_local_names(block.tail, cur_bound)
    return result


# ---- final substitution resolution --------------------------------------------------------------
#
# `_Lowerer` builds IR bottom-up exactly like `Inferencer` builds types bottom-up: a node's `.ty`
# is correct *as of* the substitution known when it was constructed, but a sibling processed later
# in the same declaration can still refine (further bind) a type variable that node's `.ty` uses.
# Substitutions only ever grow more specific, never invalidate an earlier binding, so applying a
# declaration's *final* substitution to every already-built node's `.ty` — one rewrite pass, after
# that declaration is fully lowered — is sound and gives every node its true final type.


def _resolve_ir_expr(subst: Substitution, node: IrExpr) -> IrExpr:
    match node:
        case IrInt(value, ty, span):
            return IrInt(value, apply(subst, ty), span)
        case IrFloat(value, ty, span):
            return IrFloat(value, apply(subst, ty), span)
        case IrStr(value, ty, span):
            return IrStr(value, apply(subst, ty), span)
        case IrBool(value, ty, span):
            return IrBool(value, apply(subst, ty), span)
        case IrName(name, is_global, ty, span):
            return IrName(name, is_global, apply(subst, ty), span)
        case IrUnary(op, operand, ty, span):
            return IrUnary(op, _resolve_ir_expr(subst, operand), apply(subst, ty), span)
        case IrBinary(op, left, right, ty, span):
            return IrBinary(
                op, _resolve_ir_expr(subst, left), _resolve_ir_expr(subst, right), apply(subst, ty), span
            )
        case IrIf(cond, then_b, else_b, ty, span):
            return IrIf(
                _resolve_ir_expr(subst, cond),
                _resolve_ir_block(subst, then_b),
                _resolve_ir_block(subst, else_b),
                apply(subst, ty),
                span,
            )
        case IrCall(callee, args, ty, span):
            return IrCall(
                _resolve_ir_expr(subst, callee),
                tuple(_resolve_ir_expr(subst, a) for a in args),
                apply(subst, ty),
                span,
            )
        case IrListLit(items, ty, span):
            return IrListLit(tuple(_resolve_ir_expr(subst, i) for i in items), apply(subst, ty), span)
        case IrClosure(params, param_types, body, captures, ty, span):
            return IrClosure(
                params,
                tuple(apply(subst, pt) for pt in param_types),
                _resolve_ir_expr(subst, body),
                captures,
                apply(subst, ty),
                span,
            )
        case IrBlock():
            return _resolve_ir_block(subst, node)


def _resolve_ir_block(subst: Substitution, block: IrBlock) -> IrBlock:
    stmts = tuple(_resolve_ir_stmt(subst, s) for s in block.stmts)
    tail = _resolve_ir_expr(subst, block.tail) if block.tail is not None else None
    return IrBlock(stmts, tail, apply(subst, block.ty), block.span)


def _resolve_ir_stmt(subst: Substitution, stmt: IrStmt) -> IrStmt:
    match stmt:
        case IrLet(name, value, span):
            return IrLet(name, _resolve_ir_expr(subst, value), span)
        case IrExprStmt(expr, span):
            return IrExprStmt(_resolve_ir_expr(subst, expr), span)


# ---- lowering -------------------------------------------------------------------------------------

_ARITHMETIC = {"+", "-", "*", "/", "%"}
_FLOAT_ARITHMETIC = {"+.", "-.", "*.", "/."}
_COMPARISON = {"==", "!=", "<", "<=", ">", ">="}
_LOGICAL = {"&&", "||"}


class _Lowerer:
    """Mirrors `types.Inferencer`'s tree walk (one method per `_infer_*`) closely enough that a
    change to the type checker's traversal is easy to keep in sync with, while building IR instead
    of just types. `locals_` is the set of names currently shadowing a global at this lexical
    point — threaded exactly the way `TypeEnv` is threaded, so `IrName.is_global` reflects true
    lexical shadowing rather than a blind "is this name a top-level decl" check."""

    def __init__(self, global_names: frozenset[str]) -> None:
        self._inferencer = Inferencer()
        self._global_names = global_names

    def _is_global(self, name: str, locals_: frozenset[str]) -> bool:
        return name in self._global_names and name not in locals_

    def lower_expr(self, env: TypeEnv, locals_: frozenset[str], expr: ast.Expr) -> tuple[Substitution, IrExpr]:
        match expr:
            case ast.IntLit(value, span):
                return {}, IrInt(value, T_INT, span)
            case ast.FloatLit(value, span):
                return {}, IrFloat(value, T_FLOAT, span)
            case ast.StringLit(value, span):
                return {}, IrStr(value, T_STRING, span)
            case ast.BoolLit(value, span):
                return {}, IrBool(value, T_BOOL, span)
            case ast.Ident(name, span):
                return self._lower_ident(env, locals_, name, span)
            case ast.Unary(op, operand, span):
                return self._lower_unary(env, locals_, op, operand, span)
            case ast.Binary(op, left, right, span):
                return self._lower_binary(env, locals_, op, left, right, span)
            case ast.If(cond, then_b, else_b, span):
                return self._lower_if(env, locals_, cond, then_b, else_b, span)
            case ast.Call(callee, args, span):
                return self._lower_call(env, locals_, callee, args, span)
            case ast.Lambda(params, body, span):
                return self._lower_lambda(env, locals_, params, body, span)
            case ast.ListLit(items, span):
                return self._lower_list(env, locals_, items, span)
            case ast.BlockExpr():
                return self.lower_block(env, locals_, expr)

    def _lower_ident(self, env: TypeEnv, locals_: frozenset[str], name: str, span: Span) -> tuple[Substitution, IrExpr]:
        scheme = env.bindings.get(name)
        if scheme is None:
            raise TypeCheckError(f"unbound name {name!r}", span)
        t = self._inferencer.instantiate(scheme)
        return {}, IrName(name, self._is_global(name, locals_), t, span)

    def _lower_unary(
        self, env: TypeEnv, locals_: frozenset[str], op: str, operand: ast.Expr, span: Span
    ) -> tuple[Substitution, IrExpr]:
        s1, ir_operand = self.lower_expr(env, locals_, operand)
        if op == "-":
            s2 = unify(ir_operand.ty, T_INT, span)
            return compose(s2, s1), IrUnary(op, ir_operand, T_INT, span)
        if op == "-.":
            s2 = unify(ir_operand.ty, T_FLOAT, span)
            return compose(s2, s1), IrUnary(op, ir_operand, T_FLOAT, span)
        if op == "!":
            s2 = unify(ir_operand.ty, T_BOOL, span)
            return compose(s2, s1), IrUnary(op, ir_operand, T_BOOL, span)
        raise TypeCheckError(f"unknown unary operator {op!r}", span)

    def _lower_binary(
        self, env: TypeEnv, locals_: frozenset[str], op: str, left: ast.Expr, right: ast.Expr, span: Span
    ) -> tuple[Substitution, IrExpr]:
        s1, ir_left = self.lower_expr(env, locals_, left)
        s2, ir_right = self.lower_expr(apply_env(s1, env), locals_, right)
        t1 = apply(s2, ir_left.ty)
        subst = compose(s2, s1)

        if op in _ARITHMETIC:
            subst = compose(unify(t1, T_INT, span), subst)
            subst = compose(unify(apply(subst, ir_right.ty), T_INT, span), subst)
            return subst, IrBinary(op, ir_left, ir_right, T_INT, span)
        if op in _FLOAT_ARITHMETIC:
            subst = compose(unify(t1, T_FLOAT, span), subst)
            subst = compose(unify(apply(subst, ir_right.ty), T_FLOAT, span), subst)
            return subst, IrBinary(op, ir_left, ir_right, T_FLOAT, span)
        if op in _COMPARISON:
            subst = compose(unify(t1, apply(subst, ir_right.ty), span), subst)
            return subst, IrBinary(op, ir_left, ir_right, T_BOOL, span)
        if op in _LOGICAL:
            subst = compose(unify(t1, T_BOOL, span), subst)
            subst = compose(unify(apply(subst, ir_right.ty), T_BOOL, span), subst)
            return subst, IrBinary(op, ir_left, ir_right, T_BOOL, span)
        raise TypeCheckError(f"unknown binary operator {op!r}", span)

    def _lower_if(
        self,
        env: TypeEnv,
        locals_: frozenset[str],
        cond: ast.Expr,
        then_b: ast.BlockExpr,
        else_b: ast.BlockExpr,
        span: Span,
    ) -> tuple[Substitution, IrExpr]:
        s1, ir_cond = self.lower_expr(env, locals_, cond)
        s2 = unify(ir_cond.ty, T_BOOL, span)
        subst = compose(s2, s1)
        env2 = apply_env(subst, env)

        s3, ir_then = self.lower_block(env2, locals_, then_b)
        subst = compose(s3, subst)
        env3 = apply_env(s3, env2)

        s4, ir_else = self.lower_block(env3, locals_, else_b)
        subst = compose(s4, subst)

        s5 = unify(apply(subst, ir_then.ty), apply(subst, ir_else.ty), span)
        subst = compose(s5, subst)
        return subst, IrIf(ir_cond, ir_then, ir_else, apply(subst, ir_then.ty), span)

    def _lower_call(
        self, env: TypeEnv, locals_: frozenset[str], callee: ast.Expr, args: tuple[ast.Expr, ...], span: Span
    ) -> tuple[Substitution, IrExpr]:
        subst, ir_callee = self.lower_expr(env, locals_, callee)
        ir_args: list[IrExpr] = []
        cur_env = apply_env(subst, env)
        for arg in args:
            s_arg, ir_arg = self.lower_expr(cur_env, locals_, arg)
            subst = compose(s_arg, subst)
            cur_env = apply_env(s_arg, cur_env)
            ir_args.append(ir_arg)
        arg_types = [apply(subst, a.ty) for a in ir_args]
        result = self._inferencer.fresh()
        s_unify = unify(apply(subst, ir_callee.ty), TFun(tuple(arg_types), result), span)
        subst = compose(s_unify, subst)
        return subst, IrCall(ir_callee, tuple(ir_args), apply(subst, result), span)

    def _lower_lambda(
        self, env: TypeEnv, locals_: frozenset[str], params: tuple[ast.Param, ...], body: ast.Expr, span: Span
    ) -> tuple[Substitution, IrExpr]:
        param_types = [self._inferencer.type_ann_to_type(p.type_ann) for p in params]
        inner_env = env
        inner_locals = locals_
        for p, pt in zip(params, param_types, strict=True):
            inner_env = inner_env.extend(p.name, TypeScheme(frozenset(), pt))
            inner_locals = inner_locals | {p.name}
        s_body, ir_body = self.lower_expr(inner_env, inner_locals, body)
        resolved_param_types = tuple(apply(s_body, pt) for pt in param_types)
        own_names = frozenset(p.name for p in params)
        captures = tuple(sorted(_free_local_names(ir_body, own_names)))
        ty: Type = TFun(resolved_param_types, ir_body.ty)
        closure = IrClosure(tuple(p.name for p in params), resolved_param_types, ir_body, captures, ty, span)
        return s_body, closure

    def _lower_list(
        self, env: TypeEnv, locals_: frozenset[str], items: tuple[ast.Expr, ...], span: Span
    ) -> tuple[Substitution, IrExpr]:
        if not items:
            return {}, IrListLit((), t_list(self._inferencer.fresh()), span)
        subst, ir_first = self.lower_expr(env, locals_, items[0])
        ir_items = [ir_first]
        cur_env = apply_env(subst, env)
        elem_t = ir_first.ty
        for item in items[1:]:
            s_item, ir_item = self.lower_expr(cur_env, locals_, item)
            subst = compose(s_item, subst)
            cur_env = apply_env(s_item, cur_env)
            s_unify = unify(apply(subst, elem_t), apply(subst, ir_item.ty), span)
            subst = compose(s_unify, subst)
            elem_t = apply(subst, elem_t)
            ir_items.append(ir_item)
        return subst, IrListLit(tuple(ir_items), t_list(apply(subst, elem_t)), span)

    def lower_block(self, env: TypeEnv, locals_: frozenset[str], block: ast.BlockExpr) -> tuple[Substitution, IrBlock]:
        subst: Substitution = {}
        cur_env = env
        cur_locals = locals_
        ir_stmts: list[IrStmt] = []
        for stmt in block.stmts:
            match stmt:
                case ast.LetDecl(name, type_ann, value, span):
                    s_val, ir_val = self.lower_expr(cur_env, cur_locals, value)
                    t_val = ir_val.ty
                    if type_ann is not None:
                        expected = self._inferencer.type_ann_to_type(type_ann)
                        s_check = unify(t_val, expected, span)
                        s_val = compose(s_check, s_val)
                    subst = compose(s_val, subst)
                    cur_env = apply_env(s_val, cur_env)
                    scheme = generalize(cur_env, apply(s_val, t_val))
                    cur_env = cur_env.extend(name, scheme)
                    cur_locals = cur_locals | {name}
                    ir_stmts.append(IrLet(name, ir_val, span))
                case ast.ExprStmt(expr, span):
                    s_expr, ir_expr = self.lower_expr(cur_env, cur_locals, expr)
                    subst = compose(s_expr, subst)
                    cur_env = apply_env(s_expr, cur_env)
                    ir_stmts.append(IrExprStmt(ir_expr, span))
        if block.tail is None:
            return subst, IrBlock(tuple(ir_stmts), None, T_UNIT, block.span)
        s_tail, ir_tail = self.lower_expr(cur_env, cur_locals, block.tail)
        subst = compose(s_tail, subst)
        return subst, IrBlock(tuple(ir_stmts), ir_tail, apply(subst, ir_tail.ty), block.span)

    def lower_program(self, program: ast.Program) -> IrProgram:
        env = TypeEnv()
        decls: list[IrDecl] = []
        for decl in program.decls:
            match decl:
                case ast.LetDecl(name, type_ann, value, span):
                    subst, ir_val = self.lower_expr(env, frozenset(), value)
                    t_val = ir_val.ty
                    if type_ann is not None:
                        expected = self._inferencer.type_ann_to_type(type_ann)
                        subst = compose(unify(t_val, expected, span), subst)
                    t_val = apply(subst, t_val)
                    env = apply_env(subst, env)
                    scheme = generalize(env, t_val)
                    env = env.extend(name, scheme)
                    resolved_val = _resolve_ir_expr(subst, ir_val)
                    decls.append(IrGlobalLet(name, resolved_val, t_val, span))
                case ast.FnDecl(name, params, return_type_ann, body, span):
                    if not isinstance(body, ast.BlockExpr):
                        raise TypeCheckError("function body must be a block", span)  # unreachable via the parser
                    self_type = self._inferencer.fresh()
                    body_env = env.extend(name, TypeScheme(frozenset(), self_type))
                    param_types = [self._inferencer.type_ann_to_type(p.type_ann) for p in params]
                    for p, pt in zip(params, param_types, strict=True):
                        body_env = body_env.extend(p.name, TypeScheme(frozenset(), pt))
                    param_names = frozenset(p.name for p in params)

                    s_body, ir_body = self.lower_block(body_env, param_names, body)
                    if return_type_ann is not None:
                        expected_ret = self._inferencer.type_ann_to_type(return_type_ann)
                        s_body = compose(unify(ir_body.ty, expected_ret, span), s_body)

                    fn_type = TFun(tuple(apply(s_body, pt) for pt in param_types), apply(s_body, ir_body.ty))
                    s_self = unify(apply(s_body, self_type), fn_type, span)
                    full_subst = compose(s_self, s_body)
                    final_type = apply(full_subst, fn_type)
                    assert isinstance(final_type, TFun)  # `apply` on a TFun always yields a TFun

                    env = apply_env(full_subst, env)
                    scheme = generalize(env, final_type)
                    env = env.extend(name, scheme)

                    resolved_body = _resolve_ir_block(full_subst, ir_body)
                    decls.append(
                        IrFunction(
                            name, tuple(p.name for p in params), final_type.params, final_type.ret, resolved_body, span
                        )
                    )
        return IrProgram(tuple(decls))


def lower_program(program: ast.Program) -> IrProgram:
    """Type-checks (via the same `Inferencer` machinery `types.infer_program` uses) and lowers
    `program` in one pass. A separate up-front `types.infer_program(program)` call isn't needed
    first — this *is* an equivalent type-checking pass, structurally, that additionally builds IR;
    running both would duplicate all of the unification work for no extra safety."""
    global_names = frozenset(d.name for d in program.decls)
    return _Lowerer(global_names).lower_program(program)
