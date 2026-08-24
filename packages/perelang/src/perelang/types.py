"""Hindley-Milner type inference (Algorithm W) over the AST in `ast_nodes.py`.

**Scope, stated plainly.** Arithmetic operators (`+ - * / %`) are typed monomorphically over
`Int` only — there is no numeric type class here, so plain `+` never accepts a `Float` operand.
This is not an oversight worth hiding: early ML dialects (OCaml today, even) draw the exact same
line, with a *separate* set of operators (`+. -. *. /.`) for floats rather than ad-hoc
numeric-tower coercion. Those separate operators are implemented here too (see
`_FLOAT_ARITHMETIC` below, and `docs/adr/0002-arithmetic-monomorphic-over-int.md`'s "Consequences"
note) — `Float` is therefore a real, fully arithmetic type, just never implicitly coercible with
`Int`: `1 +. 2.0` is still a type error, exactly as `1 + 2.0` is.

**Type representation**: `Type` is a `TCon | TVar | TFun` union. `TCon` covers nullary types
(`Int`, `Bool`, `Unit`, ...) and one-argument generics (`List<T>` is `TCon("List", (T,))`).
`TFun` is kept as its own variant (params + return) rather than encoded as a curried binary
`TCon("->", ...)`, matching this language's actual multi-argument function syntax directly rather
than desugaring to curried unary application everywhere generalize/instantiate would otherwise
need to unwrap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from perelang import ast_nodes as ast
from perelang.errors import Span, TypeCheckError

# ---- Types ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TCon:
    name: str
    args: tuple[Type, ...] = ()


@dataclass(frozen=True, slots=True)
class TVar:
    id: int


@dataclass(frozen=True, slots=True)
class TFun:
    params: tuple[Type, ...]
    ret: Type


Type = TCon | TVar | TFun

T_INT = TCon("Int")
T_FLOAT = TCon("Float")
T_BOOL = TCon("Bool")
T_STRING = TCon("String")
T_UNIT = TCon("Unit")


def t_list(elem: Type) -> TCon:
    return TCon("List", (elem,))


def type_to_str(t: Type) -> str:
    match t:
        case TCon(name, ()):
            return name
        case TCon(name, args):
            return f"{name}<{', '.join(type_to_str(a) for a in args)}>"
        case TVar(id_):
            return f"t{id_}"
        case TFun(params, ret):
            return f"({', '.join(type_to_str(p) for p in params)}) -> {type_to_str(ret)}"


# ---- Substitution -------------------------------------------------------------------------------

Substitution = dict[int, Type]


def apply(subst: Substitution, t: Type) -> Type:
    match t:
        case TVar(id_):
            found = subst.get(id_)
            return apply(subst, found) if found is not None else t
        case TCon(name, args):
            return TCon(name, tuple(apply(subst, a) for a in args))
        case TFun(params, ret):
            return TFun(tuple(apply(subst, p) for p in params), apply(subst, ret))


def apply_env(subst: Substitution, env: TypeEnv) -> TypeEnv:
    return TypeEnv({name: scheme.apply(subst) for name, scheme in env.bindings.items()})


def compose(newer: Substitution, older: Substitution) -> Substitution:
    """`newer` was computed after `older`; applying the composed result once must equal applying
    `older` then `newer` — so every RHS already in `older` gets `newer` applied to it too."""
    composed: Substitution = {k: apply(newer, v) for k, v in older.items()}
    composed.update(newer)
    return composed


def free_vars(t: Type) -> set[int]:
    match t:
        case TVar(id_):
            return {id_}
        case TCon(_, args):
            result: set[int] = set()
            for a in args:
                result |= free_vars(a)
            return result
        case TFun(params, ret):
            result = free_vars(ret)
            for p in params:
                result |= free_vars(p)
            return result


def occurs(var_id: int, t: Type) -> bool:
    return var_id in free_vars(t)


def unify(a: Type, b: Type, span: Span) -> Substitution:
    match (a, b):
        case (TVar(id_a), TVar(id_b)) if id_a == id_b:
            return {}
        case (TVar(id_), _):
            if occurs(id_, b):
                raise TypeCheckError(f"occurs check failed: t{id_} occurs in {type_to_str(b)}", span)
            return {id_: b}
        case (_, TVar(id_)):
            if occurs(id_, a):
                raise TypeCheckError(f"occurs check failed: t{id_} occurs in {type_to_str(a)}", span)
            return {id_: a}
        case (TCon(name_a, args_a), TCon(name_b, args_b)):
            if name_a != name_b or len(args_a) != len(args_b):
                raise TypeCheckError(f"cannot unify {type_to_str(a)} with {type_to_str(b)}", span)
            subst: Substitution = {}
            for x, y in zip(args_a, args_b, strict=True):
                subst = compose(unify(apply(subst, x), apply(subst, y), span), subst)
            return subst
        case (TFun(params_a, ret_a), TFun(params_b, ret_b)):
            if len(params_a) != len(params_b):
                raise TypeCheckError(
                    f"cannot unify {type_to_str(a)} with {type_to_str(b)}: different arity", span
                )
            subst = {}
            for x, y in zip(params_a, params_b, strict=True):
                subst = compose(unify(apply(subst, x), apply(subst, y), span), subst)
            subst = compose(unify(apply(subst, ret_a), apply(subst, ret_b), span), subst)
            return subst
        case _:
            raise TypeCheckError(f"cannot unify {type_to_str(a)} with {type_to_str(b)}", span)


# ---- Type schemes and the environment ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TypeScheme:
    """`forall vars. type` — a let-bound name's *generalized* type. Instantiating it (see
    `Inferencer.instantiate`) is what lets `let id = |x| x;` be used at both `Int` and `String`
    in the same program: each use gets its own fresh copy of the quantified variables."""

    vars: frozenset[int]
    type: Type

    def apply(self, subst: Substitution) -> TypeScheme:
        # Never substitute a scheme's own bound variables — only the free ones a substitution
        # from an *enclosing* scope could possibly refer to.
        narrowed = {k: v for k, v in subst.items() if k not in self.vars}
        return TypeScheme(self.vars, apply(narrowed, self.type))

    def free_vars(self) -> set[int]:
        return free_vars(self.type) - self.vars


@dataclass(frozen=True, slots=True)
class TypeEnv:
    bindings: dict[str, TypeScheme] = field(default_factory=dict)

    def extend(self, name: str, scheme: TypeScheme) -> TypeEnv:
        return TypeEnv({**self.bindings, name: scheme})

    def free_vars(self) -> set[int]:
        result: set[int] = set()
        for scheme in self.bindings.values():
            result |= scheme.free_vars()
        return result


def generalize(env: TypeEnv, t: Type) -> TypeScheme:
    quantified = free_vars(t) - env.free_vars()
    return TypeScheme(frozenset(quantified), t)


# ---- Inference (Algorithm W) --------------------------------------------------------------------


class Inferencer:
    def __init__(self) -> None:
        self._next_var = 0

    def fresh(self) -> TVar:
        v = TVar(self._next_var)
        self._next_var += 1
        return v

    def instantiate(self, scheme: TypeScheme) -> Type:
        fresh_subst: Substitution = {v: self.fresh() for v in scheme.vars}
        return apply(fresh_subst, scheme.type)

    def type_ann_to_type(self, ann: ast.TypeAnn | None) -> Type:
        if ann is None:
            return self.fresh()
        match ann.name, len(ann.args):
            case "Int", 0:
                return T_INT
            case "Float", 0:
                return T_FLOAT
            case "Bool", 0:
                return T_BOOL
            case "String", 0:
                return T_STRING
            case "Unit", 0:
                return T_UNIT
            case "List", 1:
                return t_list(self.type_ann_to_type(ann.args[0]))
            case _:
                raise TypeCheckError(f"unknown type {ann.name!r} (arity {len(ann.args)})", ann.span)

    # -- expressions --------------------------------------------------------------------------

    def infer_expr(self, env: TypeEnv, expr: ast.Expr) -> tuple[Substitution, Type]:
        match expr:
            case ast.IntLit():
                return {}, T_INT
            case ast.FloatLit():
                return {}, T_FLOAT
            case ast.StringLit():
                return {}, T_STRING
            case ast.BoolLit():
                return {}, T_BOOL
            case ast.Ident(name, span):
                scheme = env.bindings.get(name)
                if scheme is None:
                    raise TypeCheckError(f"unbound name {name!r}", span)
                return {}, self.instantiate(scheme)
            case ast.Unary(op, operand, span):
                return self._infer_unary(env, op, operand, span)
            case ast.Binary(op, left, right, span):
                return self._infer_binary(env, op, left, right, span)
            case ast.If(cond, then_branch, else_branch, span):
                return self._infer_if(env, cond, then_branch, else_branch, span)
            case ast.Call(callee, args, span):
                return self._infer_call(env, callee, args, span)
            case ast.Lambda(params, body, _span):
                return self._infer_lambda(env, params, body)
            case ast.ListLit(items, span):
                return self._infer_list(env, items, span)
            case ast.BlockExpr():
                return self.infer_block(env, expr)

    def _infer_unary(self, env: TypeEnv, op: str, operand: ast.Expr, span: Span) -> tuple[Substitution, Type]:
        s1, t1 = self.infer_expr(env, operand)
        if op == "-":
            s2 = unify(t1, T_INT, span)
            return compose(s2, s1), T_INT
        if op == "-.":
            s2 = unify(t1, T_FLOAT, span)
            return compose(s2, s1), T_FLOAT
        if op == "!":
            s2 = unify(t1, T_BOOL, span)
            return compose(s2, s1), T_BOOL
        raise TypeCheckError(f"unknown unary operator {op!r}", span)

    _ARITHMETIC = {"+", "-", "*", "/", "%"}
    _FLOAT_ARITHMETIC = {"+.", "-.", "*.", "/."}
    _COMPARISON = {"==", "!=", "<", "<=", ">", ">="}
    _LOGICAL = {"&&", "||"}

    def _infer_binary(
        self, env: TypeEnv, op: str, left: ast.Expr, right: ast.Expr, span: Span
    ) -> tuple[Substitution, Type]:
        s1, t1 = self.infer_expr(env, left)
        s2, t2 = self.infer_expr(apply_env(s1, env), right)
        t1 = apply(s2, t1)
        subst = compose(s2, s1)

        if op in self._ARITHMETIC:
            subst = compose(unify(t1, T_INT, span), subst)
            subst = compose(unify(apply(subst, t2), T_INT, span), subst)
            return subst, T_INT
        if op in self._FLOAT_ARITHMETIC:
            subst = compose(unify(t1, T_FLOAT, span), subst)
            subst = compose(unify(apply(subst, t2), T_FLOAT, span), subst)
            return subst, T_FLOAT
        if op in self._COMPARISON:
            subst = compose(unify(t1, t2, span), subst)
            return subst, T_BOOL
        if op in self._LOGICAL:
            subst = compose(unify(t1, T_BOOL, span), subst)
            subst = compose(unify(apply(subst, t2), T_BOOL, span), subst)
            return subst, T_BOOL
        raise TypeCheckError(f"unknown binary operator {op!r}", span)

    def _infer_if(
        self, env: TypeEnv, cond: ast.Expr, then_b: ast.BlockExpr, else_b: ast.BlockExpr, span: Span
    ) -> tuple[Substitution, Type]:
        s1, t_cond = self.infer_expr(env, cond)
        s2 = unify(t_cond, T_BOOL, span)
        subst = compose(s2, s1)
        env2 = apply_env(subst, env)

        s3, t_then = self.infer_block(env2, then_b)
        subst = compose(s3, subst)
        env3 = apply_env(s3, env2)

        s4, t_else = self.infer_block(env3, else_b)
        subst = compose(s4, subst)

        s5 = unify(apply(subst, t_then), apply(subst, t_else), span)
        subst = compose(s5, subst)
        return subst, apply(subst, t_then)

    def _infer_call(
        self, env: TypeEnv, callee: ast.Expr, args: tuple[ast.Expr, ...], span: Span
    ) -> tuple[Substitution, Type]:
        subst, t_callee = self.infer_expr(env, callee)
        arg_types: list[Type] = []
        cur_env = apply_env(subst, env)
        for arg in args:
            s_arg, t_arg = self.infer_expr(cur_env, arg)
            subst = compose(s_arg, subst)
            cur_env = apply_env(s_arg, cur_env)
            arg_types.append(apply(s_arg, t_arg))
        # Re-apply the full accumulated substitution to every argument type collected so far —
        # a later argument's inference can refine an earlier one's type variables.
        arg_types = [apply(subst, t) for t in arg_types]
        result = self.fresh()
        s_unify = unify(apply(subst, t_callee), TFun(tuple(arg_types), result), span)
        subst = compose(s_unify, subst)
        return subst, apply(subst, result)

    def _infer_lambda(
        self, env: TypeEnv, params: tuple[ast.Param, ...], body: ast.Expr
    ) -> tuple[Substitution, Type]:
        param_types = [self.type_ann_to_type(p.type_ann) for p in params]
        inner_env = env
        for p, pt in zip(params, param_types, strict=True):
            inner_env = inner_env.extend(p.name, TypeScheme(frozenset(), pt))
        s_body, t_body = self.infer_expr(inner_env, body)
        param_types = [apply(s_body, pt) for pt in param_types]
        return s_body, TFun(tuple(param_types), t_body)

    def _infer_list(self, env: TypeEnv, items: tuple[ast.Expr, ...], span: Span) -> tuple[Substitution, Type]:
        if not items:
            return {}, t_list(self.fresh())
        subst, elem_t = self.infer_expr(env, items[0])
        cur_env = apply_env(subst, env)
        for item in items[1:]:
            s_item, t_item = self.infer_expr(cur_env, item)
            subst = compose(s_item, subst)
            cur_env = apply_env(s_item, cur_env)
            s_unify = unify(apply(subst, elem_t), apply(subst, t_item), span)
            subst = compose(s_unify, subst)
            elem_t = apply(subst, elem_t)
        return subst, t_list(apply(subst, elem_t))

    # -- blocks and let-polymorphism ------------------------------------------------------------

    def infer_block(self, env: TypeEnv, block: ast.BlockExpr) -> tuple[Substitution, Type]:
        subst: Substitution = {}
        cur_env = env
        for stmt in block.stmts:
            match stmt:
                case ast.LetDecl(name, type_ann, value, span):
                    s_val, t_val = self.infer_expr(cur_env, value)
                    if type_ann is not None:
                        expected = self.type_ann_to_type(type_ann)
                        s_check = unify(t_val, expected, span)
                        s_val = compose(s_check, s_val)
                        t_val = apply(s_check, t_val)
                    subst = compose(s_val, subst)
                    cur_env = apply_env(s_val, cur_env)
                    scheme = generalize(cur_env, t_val)
                    cur_env = cur_env.extend(name, scheme)
                case ast.ExprStmt(expr, _span):
                    s_expr, _t = self.infer_expr(cur_env, expr)
                    subst = compose(s_expr, subst)
                    cur_env = apply_env(s_expr, cur_env)
        if block.tail is None:
            return subst, T_UNIT
        s_tail, t_tail = self.infer_expr(cur_env, block.tail)
        subst = compose(s_tail, subst)
        return subst, apply(subst, t_tail)

    # -- top level --------------------------------------------------------------------------

    def infer_program(self, program: ast.Program) -> dict[str, Type]:
        """Returns each top-level name's final, generalized type. Functions support recursion via
        the standard HM `letrec` trick: the function's own name is bound to a fresh type variable
        *while its body is being inferred* (so a recursive call type-checks against a not-yet-known
        but consistent type), which is unified with the function's actual inferred signature
        afterward and *then* generalized — so recursion inside the body is monomorphic (every
        recursive call must agree on one instantiation) while uses of the function *elsewhere*
        are fully polymorphic, exactly matching how every ML-family language's `let rec` behaves.
        """
        env = TypeEnv()
        result: dict[str, Type] = {}
        for decl in program.decls:
            match decl:
                case ast.LetDecl(name, type_ann, value, span):
                    subst, t_val = self.infer_expr(env, value)
                    if type_ann is not None:
                        expected = self.type_ann_to_type(type_ann)
                        subst = compose(unify(t_val, expected, span), subst)
                        t_val = apply(subst, t_val)
                    env = apply_env(subst, env)
                    scheme = generalize(env, t_val)
                    env = env.extend(name, scheme)
                    result[name] = apply(subst, t_val)
                case ast.FnDecl(name, params, return_type_ann, body, span):
                    self_type = self.fresh()
                    body_env = env.extend(name, TypeScheme(frozenset(), self_type))
                    param_types = [self.type_ann_to_type(p.type_ann) for p in params]
                    for p, pt in zip(params, param_types, strict=True):
                        body_env = body_env.extend(p.name, TypeScheme(frozenset(), pt))

                    s_body, t_body = self.infer_expr(body_env, body)
                    if return_type_ann is not None:
                        expected_ret = self.type_ann_to_type(return_type_ann)
                        s_body = compose(unify(t_body, expected_ret, span), s_body)
                        t_body = apply(s_body, t_body)

                    fn_type = TFun(tuple(apply(s_body, pt) for pt in param_types), t_body)
                    s_self = unify(apply(s_body, self_type), fn_type, span)
                    full_subst = compose(s_self, s_body)
                    final_type = apply(full_subst, fn_type)

                    env = apply_env(full_subst, env)
                    scheme = generalize(env, final_type)
                    env = env.extend(name, scheme)
                    result[name] = final_type
        return result


def infer_program(program: ast.Program) -> dict[str, Type]:
    return Inferencer().infer_program(program)


def infer_expr(env: TypeEnv, expr: ast.Expr) -> Type:
    inferencer = Inferencer()
    subst, t = inferencer.infer_expr(env, expr)
    return apply(subst, t)
