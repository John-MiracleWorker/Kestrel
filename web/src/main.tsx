import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { ApplicationProvider } from "./app/ApplicationContext";
import { installTheme } from "./design/theme";
import "./design/tokens.css";
import "./design/typography.css";
import "./styles.css";
import "./design/design-system.css";

installTheme();

createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <ApplicationProvider>
      <App />
    </ApplicationProvider>
  </React.StrictMode>
);
