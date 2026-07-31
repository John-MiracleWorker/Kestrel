import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { ApplicationProvider } from "./app/ApplicationContext";
import "./styles.css";

createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <ApplicationProvider>
      <App />
    </ApplicationProvider>
  </React.StrictMode>
);
