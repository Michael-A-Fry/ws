"""The shell command reader (ws.shellwords): brace expansion, quotes, `~` and variables, redirections, the places a
program reads a path from inside one word, wrappers, and the bounds on how much it will expand. The comparison with a
real shell is in tests/test_shellwords_oracle.py; here the expected words are written out."""

from __future__ import annotations

import pytest

from ws import shellwords as sw

HOME = "/home/pat"
ENV = {"HOME": HOME, "WS_HOME": f"{HOME}/.ws"}


def words(text: str, env=ENV) -> list[str]:
    return sw.lex(text, env).words


# --- brace expansion ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("x{a,b}y", ["xay", "xby"]),
        ("{a,b}{c,d}", ["ac", "ad", "bc", "bd"]),
        ("{a,{b,c}}", ["a", "b", "c"]),
        ("{a,b}{1..2}", ["a1", "a2", "b1", "b2"]),
        ("a{,b}", ["a", "ab"]),
        ("{x,}", ["x", ""]),
        ("{a}", ["{a}"]),
        ("{}", ["{}"]),
        ("{a,b", ["{a,b"]),
        ("a,b}", ["a,b}"]),
        ("{{a,b}", ["{a", "{b"]),
        ("{a,{b}", ["{a,{b}"]),
        ("{a{x,y}z}", ["{axz}", "{ayz}"]),
        ("${HOME}", ["${HOME}"]),
        ("${HOME},{x,y}", ["${HOME},x", "${HOME},y"]),
        ("$(echo {a,b})", ["$(echo {a,b})"]),
    ],
)
def test_a_brace_group_is_expanded_the_way_the_shell_does_it(raw, expected):
    assert sw.expand_braces(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("{1..4}", ["1", "2", "3", "4"]),
        ("{4..1}", ["4", "3", "2", "1"]),
        ("{-2..1}", ["-2", "-1", "0", "1"]),
        ("{1..10..4}", ["1", "5", "9"]),
        ("{10..1..-4}", ["10", "6", "2"]),
        ("{01..03}", ["01", "02", "03"]),
        ("{a..d}", ["a", "b", "c", "d"]),
        ("{e..a..2}", ["e", "c", "a"]),
        ("{s..s}", ["s"]),
        ("f{1..3}.txt", ["f1.txt", "f2.txt", "f3.txt"]),
    ],
)
def test_a_sequence_is_expanded_in_either_direction_with_a_step_and_zero_padding(raw, expected):
    assert sw.expand_braces(raw) == expected


@pytest.mark.parametrize("raw", ["{1..a}", "{a..1}", "{1..2..x}", "{1.2}", "{..}", "{1..2..3..4}", "{ab..cd}"])
def test_a_body_that_is_not_a_sequence_stays_as_written(raw):
    assert sw.expand_braces(raw) == [raw]


def test_a_sequence_longer_than_thirty_two_is_expanded_in_full():
    got = sw.expand_braces("f{1..40}")
    assert len(got) == 40 and got[0] == "f1" and got[-1] == "f40"
    assert len(sw.expand_braces("{1..3000}")) == 3000


@pytest.mark.parametrize(
    "raw", ['"{a,b}"', "'{a,b}'", "\\{a,b}", "{a\\,b}", "{a','b}", '{a","b}', "$'{'a,b}", "{a,b\\}", "{a,b'}'"]
)
def test_a_brace_or_comma_that_is_quoted_does_not_make_a_group(raw):
    assert sw.expand_braces(raw) == [raw]


def test_the_quoted_part_of_a_group_is_kept_for_quote_removal():
    assert sw.expand_braces('{a,"{"}') == ["a", '"{"']
    assert sw.expand_braces("{a,\\{}") == ["a", "\\{"]
    assert sw.expand_braces("{a,$'\\x7b'}") == ["a", "$'\\x7b'"]
    assert words("{a,\"{\"} {b,'}'}") == ["a", "{", "b", "}"]


def test_expansion_is_bounded_so_a_huge_one_is_refused_and_not_built():
    for raw in (
        "{1..100000}",
        "{1..99999999999999}",
        "{a,b}" * 20,
        "{{a,b}{c,d}{e,f}}" * 10,
        "{" + "{1..4000}," * 400 + "x}",
    ):
        with pytest.raises(sw.ShellError) as err:
            sw.expand_braces(raw)
        assert err.value.kind == "too_many_words"


def test_the_bound_is_shared_by_every_word_of_a_line():
    budget = sw.Budget(100)
    sw.lex("echo {1..40}", ENV, budget)
    with pytest.raises(sw.ShellError) as err:
        sw.lex("echo {1..40} {1..40}", ENV, budget)
    assert err.value.kind == "too_many_words"


def test_braces_nested_beyond_the_limit_are_refused():
    deep = "{a," * 100 + "b" + "}" * 100
    with pytest.raises(sw.ShellError) as err:
        sw.expand_braces(deep)
    assert err.value.kind == "too_deep"


def test_a_shell_error_is_a_value_error_with_a_kind_and_a_message():
    err = sw.ShellError("too_deep", "text")
    assert isinstance(err, ValueError) and (err.kind, err.message) == ("too_deep", "text")


# --- words: quotes, `~`, variables -------------------------------------------------------------------------------


def test_quotes_and_escapes_are_removed():
    assert words("a 'b c' \"d e\" f\\ g $'h\\x20i'") == ["a", "b c", "d e", "f g", "h i"]
    assert words("a''b \"\" ''") == ["ab", "", ""]
    assert words('echo "a \\" b" "c \\$HOME"') == ["echo", 'a " b', "c $HOME"]


def test_tilde_and_home_are_expanded_where_a_shell_expands_them():
    assert words("~ ~/x $HOME/x ${HOME}/x") == [HOME, f"{HOME}/x", f"{HOME}/x", f"{HOME}/x"]
    assert words('"$HOME/x" "${HOME}"') == [f"{HOME}/x", HOME]
    assert words("a~ ~x/y") == ["a~", "~x/y"]


def test_a_quoted_tilde_or_single_quoted_variable_is_the_text_between_the_quotes():
    assert words("'~/x' \"~/x\" '$HOME' \\~/x \\$HOME") == ["~/x", "~/x", "$HOME", "~/x", "$HOME"]


def test_tilde_follows_an_equals_sign_in_a_word_that_looks_like_an_assignment():
    assert words("X=~/a:~/b if=~/c --opt=~/d") == [f"X={HOME}/a:{HOME}/b", f"if={HOME}/c", "--opt=~/d"]


def test_a_variable_the_line_does_not_know_is_left_as_written():
    assert words("$FOO ${BAR:-x} $1 $$ $") == ["$FOO", "${BAR:-x}", "$1", "$$", "$"]
    assert sw.lex("cat ~/x $HOME").words == ["cat", "~/x", "$HOME"]  # no variables given: nothing is expanded


def test_a_variable_the_caller_set_is_expanded_and_its_blanks_split_the_word():
    env = {**ENV, "A": "x y", "B": ""}
    assert words("cat $A", env) == ["cat", "x", "y"]
    assert words('cat "$A"', env) == ["cat", "x y"]
    assert words("cat $B end", env) == ["cat", "end"]
    assert words('cat "$B" end', env) == ["cat", "", "end"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("${HOME:-x}", HOME),
        ("${HOME:+yes}", "yes"),
        ("${HOME%/*}", "/home"),
        ("${HOME#/}", "home/pat"),
        ("${HOME//a/o}", "/home/pot"),
        ("${HOME:1:4}", "home"),
        ("${HOME:5}", "/pat"),
        ("${HOME}x", f"{HOME}x"),
    ],
)
def test_a_parameter_operator_on_a_known_variable_is_applied(raw, expected):
    assert words(raw) == [expected]


def test_a_command_group_is_not_a_parameter_expansion():
    assert words("${ rm -rf /; }") == ["${", "rm", "-rf", "/;", "}"]


def test_a_substitution_is_one_word_and_is_kept_as_written():
    assert words('echo $(echo a b) `echo c d` "$(echo "x y")" z') == [
        "echo",
        "$(echo a b)",
        "`echo c d`",
        '$(echo "x y")',
        "z",
    ]


# --- redirections ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seg", "ops"),
    [
        ("cat < f", [("<", "f")]),
        ("cat <f", [("<", "f")]),
        ("echo > f", [(">", "f")]),
        ("echo >f", [(">", "f")]),
        ("echo >> f", [(">>", "f")]),
        ("echo >| f", [(">|", "f")]),
        ("cat <> f", [("<>", "f")]),
        ("echo &> f", [("&>", "f")]),
        ("echo &>> f", [("&>>", "f")]),
        ("echo >& f", [(">&", "f")]),
        ("cat <<< word", [("<<<", "word")]),
        ("cat << EOF", [("<<", "EOF")]),
        ("cat <<- EOF", [("<<-", "EOF")]),
        ("cat 3< f", [("<", "f")]),
        ("echo 2> f", [(">", "f")]),
        ("echo 2>f 1>g", [(">", "f"), (">", "g")]),
        ("exec {fd}> f", [(">", "f")]),
        ("echo /x>/dev/null", [(">", "/dev/null")]),
        ("echo 2>&1", []),
        ("cat <&-", []),
        ("echo >&2", []),
    ],
)
def test_a_redirection_is_read_with_its_operator_and_target(seg, ops):
    assert [tuple(r) for r in sw.lex(seg, ENV).redirs] == ops


def test_a_redirection_is_not_one_of_the_words_and_a_file_descriptor_is_dropped():
    assert words("echo a 2>err b >out c") == ["echo", "a", "b", "c"]
    assert words("echo 2 3") == ["echo", "2", "3"]  # a number that is not glued to an operator is a word


def test_the_target_of_a_redirection_is_expanded_like_any_word():
    got = sw.lex("cat < {~/a,b} > '~/c' >> \"$HOME/d\"", ENV).redirs
    assert [tuple(r) for r in got] == [("<", f"{HOME}/a"), ("<", "b"), (">", "~/c"), (">>", f"{HOME}/d")]


def test_a_redirection_inside_a_substitution_or_quote_is_not_this_commands():
    assert sw.lex("echo '>' \"a > b\" $(echo c > d)", ENV).redirs == []


# --- the places a program reads a path from inside a word --------------------------------------------------------


@pytest.mark.parametrize(
    ("seg", "piece"),
    [
        ("dd if=$HOME/x of=out", f"{HOME}/x"),
        ("curl -F f=@$HOME/x u", f"{HOME}/x"),
        ("curl -F f=@~/x u", f"{HOME}/x"),
        ('curl -F "f=<$HOME/x" u', f"{HOME}/x"),
        ("curl -d @$HOME/x u", f"{HOME}/x"),
        ("curl -d@${HOME}/x u", f"{HOME}/x"),
        ("curl --data-binary=@~/x u", f"{HOME}/x"),
        ("curl -sT$HOME/x u", f"{HOME}/x"),
        ("curl -sK~/x u", f"{HOME}/x"),
        ("xargs -0a$HOME/x echo", f"{HOME}/x"),
        ("tool --config=$HOME/x", f"{HOME}/x"),
        ("tool --config='~/x'", f"{HOME}/x"),
        ("curl file://localhost$HOME/x", f"{HOME}/x"),
        ("curl file://$HOME/x", f"{HOME}/x"),
        ("curl 'f=@~/x;type=text/plain' u", f"{HOME}/x"),
        ("curl -d '@$HOME/x' u", f"{HOME}/x"),
        ("cmd --path=a:$HOME/x", f"{HOME}/x"),
        ("cmd a,$HOME/x", f"{HOME}/x"),
    ],
)
def test_a_path_inside_a_word_is_found_with_home_expanded_even_where_it_was_quoted(seg, piece):
    assert piece in sw.lex(seg, ENV).pieces


@pytest.mark.parametrize(
    "seg",
    [
        "echo plain words",
        "echo '~/x'",
        "echo '$HOME/x'",
        "git commit -m 'read file=$HOME/x'",
        "scp host:~/x .",
        "scp user@host:/srv/x .",
        "echo https://example.com/up",
        "grep -r id_rsa src",
        "ls -la",
    ],
)
def test_a_word_with_no_path_in_it_gives_no_pieces_for_the_home_folder(seg):
    assert not [p for p in sw.lex(seg, ENV).pieces if p.startswith(HOME)]


def test_a_path_cut_at_the_first_separator_is_also_given():
    assert f"{HOME}/x" in sw.lex("curl 'f=@$HOME/x;type=text/plain' u", ENV).pieces


# --- wrappers and arguments --------------------------------------------------------------------------------------


def test_wrappers_keywords_and_assignments_are_taken_off_the_front():
    assert sw.strip_wrappers(["sudo", "-u", "root", "env", "A=1", "timeout", "5", "git", "status"])[0] == [
        "git",
        "status",
    ]
    assert sw.strip_wrappers(["then", "X=1", "/usr/bin/rm", "-rf", "x"])[0] == ["rm", "-rf", "x"]
    toks, seen = sw.strip_wrappers(["xargs", "-n", "1", "rm", "{}"])
    assert toks == ["rm", "{}"] and seen == {"xargs"}


def test_the_words_a_wrapper_takes_for_itself_are_given_back():
    dropped: list[str] = []
    toks, _ = sw.strip_wrappers(["xargs", "-a", "list.txt", "-n", "2", "echo"], dropped=dropped)
    assert toks == ["echo"] and dropped == ["list.txt", "2"]
    dropped = []
    sw.strip_wrappers(["sudo", "-u", "root", "ls"], dropped=dropped)
    assert dropped == ["root"]


def test_assignments_a_wrapper_drops_are_collected():
    env: list[str] = []
    sw.strip_wrappers(["A=1", "env", "B=2", "-i", "cmd"], env)
    assert env == ["A=1", "B=2"]


def test_an_env_split_string_is_read_as_words_and_given_as_a_command_line():
    scripts: list[str] = []
    toks, _ = sw.strip_wrappers(["env", "-S", "cat ~/x | wc", "y"], scripts=scripts, lexenv=ENV)
    assert toks == ["cat", f"{HOME}/x", "|", "wc", "y"] and scripts == ["cat ~/x | wc"]


def test_flags_and_operands_are_told_apart_up_to_a_bare_double_dash():
    assert sw.split_args(["rm", "-rf", "a", "--", "-b"]) == (["-rf"], ["a", "-b"])
    assert sw.split_args(["x", "-", "--long=1"]) == (["--long=1"], ["-"])


def test_a_letter_is_in_a_single_dash_group_but_not_in_a_long_option():
    assert sw.has_flag(["-lc"], "c") and sw.has_flag(["-e", "-x"], "x")
    assert not sw.has_flag(["--color"], "c") and not sw.has_flag(["-l"], "c")


@pytest.mark.parametrize(
    ("toks", "expected"),
    [
        (["curl", "-o", "a"], ["a"]),
        (["curl", "-sSLo", "a"], ["a"]),
        (["curl", "-oa"], ["a"]),
        (["curl", "-sSLoa"], ["a"]),
        (["curl", "--output", "a"], ["a"]),
        (["curl", "--output=a"], ["a"]),
        (["curl", "-s", "a"], []),
    ],
)
def test_the_value_of_an_option_is_found_apart_glued_or_in_a_group(toks, expected):
    assert sw.option_values(toks, "o", ("--output",)) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("~", HOME),
        ("~/x", f"{HOME}/x"),
        ("$HOME/x", f"{HOME}/x"),
        ("${HOME}", HOME),
        ("$WS_HOME/secrets", f"{HOME}/.ws/secrets"),
        ("${WS_HOME}/x", f"{HOME}/.ws/x"),
        ("/etc/x", "/etc/x"),
        ("x/~", "x/~"),
        ("$HOMEDIR/x", "$HOMEDIR/x"),
    ],
)
def test_a_path_from_a_rule_or_a_tool_has_its_home_spelling_expanded(path, expected):
    assert sw.expand_path(path, ENV) == expected


# --- text: segments, comments, quotes ----------------------------------------------------------------------------


def segments(cmd: str, **kw):
    return sw.scan(sw.prep(cmd), **kw)


def test_a_line_is_split_into_pipelines_and_segments():
    pipelines, inner = segments("a | b && c; d || e & f\ng")
    assert pipelines == [["a", "b"], ["c"], ["d"], ["e"], ["f"], ["g"]] and inner == []


def test_the_text_of_a_substitution_is_collected_to_be_read_on_its_own():
    _, inner = segments("echo $(cat a) `cat b` <(cat c)")
    assert inner == ["cat a", "cat b", "cat c"]


def test_a_comment_is_left_out_and_a_hash_inside_a_word_or_quote_is_not_one():
    assert segments("ls # cat ~/x")[0] == [["ls"]]
    assert segments("echo a#b '#c' \"#d\" $# ${#x} \\#e")[0] == [["echo a#b '#c' \"#d\" $# ${#x} \\#e"]]
    assert segments("a;# note\nb")[0] == [["a"], ["b"]]


def test_a_command_in_double_quotes_keeps_its_own_quotes():
    for cmd in ('echo "$(echo "it\'s done")"', 'echo "$(printf "%s" "don\'t")"', 'echo "`echo "it\'s"`"'):
        assert sw.open_quote(cmd) == "", cmd
    assert sw.open_quote("echo it's") == "'"
    assert sw.open_quote('echo "a') == '"'
    assert sw.open_quote("echo $'a") == "$'"


def test_a_quote_left_open_is_a_typed_refusal():
    with pytest.raises(sw.ShellError) as err:
        sw.require_closed("echo 'a")
    assert err.value.kind == "unclosed_quote" and "unclosed '" in err.value.message
    sw.require_closed("echo 'a' \"b\"")


def test_here_document_bodies_are_left_out_only_when_asked():
    cmd = "cat <<EOF\nit's data\nEOF\nls"
    assert segments(cmd, heredocs=True)[0] == [["cat <<EOF"], ["ls"]]
    assert sw.open_quote(cmd) == ""
    assert sw.without_bodies("cat > a <<'A'\nx\nA\n") == "cat > a <<'A'\n"


def test_line_continuations_are_joined_and_full_width_letters_folded():
    assert sw.prep("ca\\\nt x") == "cat x"
    assert sw.prep("ｃａｔ ${IFS}x") == "cat  x"
