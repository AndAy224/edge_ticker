import { render } from "preact";
import { App } from "./app";
import "./style.css";
import { connectWs, loadConfig, refreshHealth } from "./state";

loadConfig();
refreshHealth();
setInterval(refreshHealth, 5000);
connectWs();

render(<App />, document.getElementById("app")!);
