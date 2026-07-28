from src.widget import Widget, helper


def test_widget_render() -> None:
    assert Widget().render() == helper("ready")
