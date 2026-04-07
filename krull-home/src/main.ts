// theme.css must load first so variables are defined when globals.css reads them
import "./styles/theme.css";
import "./styles/globals.css";
import { defineRoute, mountRouter } from "./lib/router";
import { HomePage } from "./pages/home";
import { SettingsPage } from "./pages/settings";
import { LibraryPage } from "./pages/library";
import { AboutPage } from "./pages/about";

const app = document.getElementById("app")!;

defineRoute("/", () => HomePage());
defineRoute("/settings", () => SettingsPage());
defineRoute("/library", () => LibraryPage());
defineRoute("/about", () => AboutPage());

void mountRouter(app);
