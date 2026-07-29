use std::fmt::Display;

pub struct RustWidget;

pub fn show_widget(value: impl Display) -> String {
    format!("{value}")
}
