"""JSON that ``json.loads`` refuses, for tests of readers that must refuse it cleanly."""

#: Nesting past the decoder's recursion limit (RecursionError) and an integer past the digit limit (ValueError).
UNDECODABLE_JSON = {
    "nested": '{"schema": ' + "[" * 100_000 + "]" * 100_000 + "}",
    "a-number-too-long": '{"schema": 1' + "0" * 5000 + "}",
}
