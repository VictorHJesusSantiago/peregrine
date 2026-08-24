from perelang.bytecode import BytecodeProgram, FunctionProto, OpCode, compile_program
from perelang.ir import lower_program
from perelang.parser import parse_program


def compile_src(src: str) -> BytecodeProgram:
    return compile_program(lower_program(parse_program(src)))


def ops(proto: FunctionProto) -> list[OpCode]:
    return [instr.op for instr in proto.code]


class TestLiteralsAndConstants:
    def test_int_literal_compiles_to_const(self) -> None:
        program = compile_src("let x = 42;")
        assert ops(program.init) == [OpCode.CONST, OpCode.STORE_GLOBAL, OpCode.PUSH_UNIT]
        assert 42 in program.init.constants

    def test_last_global_tracks_most_recent_top_level_let(self) -> None:
        program = compile_src("let a = 1; let b = 2; let c = 3;")
        assert program.last_global == "c"

    def test_program_with_no_globals_has_no_last_global(self) -> None:
        program = compile_src("fn f() -> Int { 1 }")
        assert program.last_global is None


class TestFunctions:
    def test_function_registered_by_name_with_correct_arity(self) -> None:
        program = compile_src("fn add(a, b) -> Int { a + b }")
        assert "add" in program.functions
        assert program.functions["add"].arity == 2

    def test_function_params_compile_to_load_local(self) -> None:
        program = compile_src("fn add(a, b) -> Int { a + b }")
        proto = program.functions["add"]
        assert ops(proto) == [OpCode.LOAD_LOCAL, OpCode.LOAD_LOCAL, OpCode.ADD]
        # `a` is slot 0, `b` is slot 1, matching declaration order.
        assert proto.code[0].arg == 0
        assert proto.code[1].arg == 1

    def test_recursive_call_compiles_to_load_global_of_its_own_name(self) -> None:
        program = compile_src("fn fact(n) -> Int { if n <= 1 { 1 } else { n * fact(n - 1) } }")
        proto = program.functions["fact"]
        assert OpCode.CALL in ops(proto)
        assert OpCode.LOAD_GLOBAL in ops(proto)


class TestLetBindingsAndScoping:
    def test_let_binding_compiles_to_store_then_load(self) -> None:
        program = compile_src("fn f() -> Int { let x = 5; x }")
        proto = program.functions["f"]
        assert ops(proto) == [OpCode.CONST, OpCode.STORE_LOCAL, OpCode.LOAD_LOCAL]
        assert proto.code[1].arg == proto.code[2].arg

    def test_shadowing_in_sibling_if_branches_resolves_independently(self) -> None:
        # Each branch's `let y` is its own binding — compiling the `else` branch must not see the
        # slot the `then` branch's `y` was assigned (a flat, non-scope-stacked compiler would get
        # this wrong: see `bytecode.py`'s module docstring on the scope stack).
        program = compile_src("""
            fn f(cond: Bool, y: Int) -> Int {
              if cond { let y = 1; y } else { y }
            }
        """)
        proto = program.functions["f"]
        # `y` the parameter is slot 1 (cond is slot 0); the `let y` inside `then` must get a
        # *different* slot, and the `else` branch's bare `y` must still load the parameter's slot.
        load_locals = [instr.arg for instr in proto.code if instr.op is OpCode.LOAD_LOCAL]
        assert 1 in load_locals  # the else-branch reference to the outer parameter

    def test_num_locals_counts_params_and_lets(self) -> None:
        program = compile_src("fn f(a) -> Int { let b = 1; let c = 2; a }")
        proto = program.functions["f"]
        assert proto.num_locals == 3


class TestControlFlow:
    def test_if_compiles_to_conditional_jump(self) -> None:
        program = compile_src("let x = if true { 1 } else { 2 };")
        assert OpCode.JUMP_IF_FALSE in ops(program.init)
        assert OpCode.JUMP in ops(program.init)

    def test_jump_targets_are_patched_to_real_indices(self) -> None:
        program = compile_src("let x = if true { 1 } else { 2 };")
        code = program.init.code
        for instr in code:
            if instr.op in (OpCode.JUMP, OpCode.JUMP_IF_FALSE, OpCode.JUMP_IF_TRUE):
                assert instr.arg != -1
                assert 0 <= instr.arg <= len(code)

    def test_logical_and_short_circuits_via_jump_not_a_binary_op(self) -> None:
        # There is no `AND`/`OR` opcode at all in `OpCode` (see its definition) — `&&`/`||` only
        # ever compile to jumps, precisely so the right operand can be skipped unevaluated.
        program = compile_src("let x = true && false;")
        assert OpCode.JUMP_IF_FALSE in ops(program.init)
        assert not hasattr(OpCode, "AND")

    def test_logical_or_short_circuits_via_jump(self) -> None:
        program = compile_src("let x = true || false;")
        assert OpCode.JUMP_IF_TRUE in ops(program.init)


class TestClosures:
    def test_lambda_compiles_to_make_closure(self) -> None:
        program = compile_src("let id = |x| x;")
        assert OpCode.MAKE_CLOSURE in ops(program.init)

    def test_non_capturing_lambda_proto_has_no_capture_names(self) -> None:
        program = compile_src("let id = |x| x;")
        proto_const = next(c for c in program.init.constants if isinstance(c, FunctionProto))
        assert proto_const.capture_names == ()

    def test_capturing_lambda_proto_records_capture_names_and_loads_them_before_make_closure(self) -> None:
        program = compile_src("let make_adder = |x| |y| x + y;")
        outer_proto = next(c for c in program.init.constants if isinstance(c, FunctionProto))
        inner_proto = next(c for c in outer_proto.constants if isinstance(c, FunctionProto))
        assert inner_proto.capture_names == ("x",)
        # The outer lambda's body must load `x` (its own param, a local) immediately before
        # bundling the inner closure.
        outer_ops = ops(outer_proto)
        make_closure_idx = outer_ops.index(OpCode.MAKE_CLOSURE)
        assert outer_ops[make_closure_idx - 1] == OpCode.LOAD_LOCAL


class TestFloatArithmeticReusesIntOpcodes:
    """`+. -. *. /.` compile to the exact same `OpCode`s as their Int counterparts (see
    `bytecode.py`'s `_BINOP` — `vm.py` dispatches on the Python runtime type of the operands, not
    on which source operator produced them), so there is deliberately no `FADD`/`FSUB`/etc. opcode
    to test for — this class proves that reuse, not a separate instruction set."""

    def test_float_addition_compiles_to_the_same_add_opcode_as_int(self) -> None:
        program = compile_src("let x = 1.0 +. 2.0;")
        assert ops(program.init) == [OpCode.CONST, OpCode.CONST, OpCode.ADD, OpCode.STORE_GLOBAL, OpCode.PUSH_UNIT]
        assert 1.0 in program.init.constants
        assert 2.0 in program.init.constants

    def test_all_four_float_operators_map_to_their_int_counterpart_opcode(self) -> None:
        expected = {"+.": OpCode.ADD, "-.": OpCode.SUB, "*.": OpCode.MUL, "/.": OpCode.DIV}
        for op, opcode in expected.items():
            program = compile_src(f"let x = 1.0 {op} 2.0;")
            assert opcode in ops(program.init)

    def test_float_unary_negation_compiles_to_neg(self) -> None:
        program = compile_src("let x = -.5.0;")
        assert OpCode.NEG in ops(program.init)


class TestInstructionSpans:
    """Every compiled `Instr` carries the span of the IR node (and, transitively, the AST node)
    that produced it — see `bytecode.py`'s `_current_span` mechanism. `vm.py`'s runtime errors
    depend on this being correct; this is the compile-time half of that proof."""

    def test_binary_op_instruction_gets_the_whole_binary_expressions_span_not_the_last_operand(self) -> None:
        program = compile_src("let x = 111 / 2;")
        div_instr = next(i for i in program.init.code if i.op is OpCode.DIV)
        # "let x = 111 / 2;" -> "111 / 2" spans offsets 8..15; the DIV instruction's span must cover
        # the *whole* expression, not just trail off at "2"'s own narrower span.
        assert div_instr.span.start == 8
        assert div_instr.span.end == 15

    def test_no_instruction_is_left_with_the_placeholder_span(self) -> None:
        from perelang.bytecode import NO_SPAN

        program = compile_src("fn f(n: Int) -> Int { if n <= 1 { 1 } else { n * f(n - 1) } }")
        proto = program.functions["f"]
        assert all(instr.span != NO_SPAN for instr in proto.code)


class TestLists:
    def test_list_literal_compiles_to_make_list_with_item_count(self) -> None:
        program = compile_src("let xs = [1, 2, 3];")
        instr = next(i for i in program.init.code if i.op is OpCode.MAKE_LIST)
        assert instr.arg == 3

    def test_empty_list_compiles_to_make_list_zero(self) -> None:
        program = compile_src("let xs = [];")
        instr = next(i for i in program.init.code if i.op is OpCode.MAKE_LIST)
        assert instr.arg == 0
