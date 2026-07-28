package example;

import java.util.Objects;

public final class JavaWidget {
    public String render(String value) {
        return Objects.requireNonNull(value);
    }
}
