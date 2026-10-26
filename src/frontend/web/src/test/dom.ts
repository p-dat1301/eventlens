// Preloaded via bunfig.toml: react-dom must load after the DOM globals exist,
// otherwise it disables input-event support at module init.
import { GlobalRegistrator } from "@happy-dom/global-registrator"

if (!GlobalRegistrator.isRegistered) {
  GlobalRegistrator.register({ url: "http://localhost/" })
}

Reflect.set(globalThis, "IS_REACT_ACT_ENVIRONMENT", true)
