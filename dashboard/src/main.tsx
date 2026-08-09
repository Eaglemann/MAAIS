import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { MissionControlApp } from "./App";
import { initializeBrowserObservability } from "./observability";
import "./styles.css";

initializeBrowserObservability();

const root = document.getElementById("root");
if (!root) throw new Error("Mission Control root element is missing");

createRoot(root).render(
  <StrictMode>
    <MissionControlApp />
  </StrictMode>,
);
