from utils.log_sanitizer import sanitize_log_value


def test_sanitize_log_value_escapes_control_characters():
    assert sanitize_log_value("recipe-1\nFORGED\r\x00") == "recipe-1\\x0aFORGED\\x0d\\x00"


def test_sanitize_log_value_escapes_unicode_line_breaks():
    assert (
        sanitize_log_value("first\x85second\u2028third\u2029fourth")
        == "first\\x85second\\u2028third\\u2029fourth"
    )


def test_sanitize_log_value_caps_untrusted_text():
    assert sanitize_log_value("abcdef", max_length=3) == "abc"
