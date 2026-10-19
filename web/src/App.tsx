import { useEffect, useState } from "react";
import { Layout } from "./components/Layout";
import { Dashboard } from "./pages/Dashboard";
import { Rules } from "./pages/Rules";
import { Screener } from "./pages/Screener";
import { SymbolPage } from "./pages/SymbolPage";

export function App() {
  const [path, setPath] = useState(window.location.pathname);

  useEffect(() => {
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  function navigate(next: string) {
    if (next === window.location.pathname) {
      setPath(next);
      return;
    }
    window.history.pushState({}, "", next);
    setPath(next);
  }

  const symbol = symbolFromPath(path);

  return (
    <Layout path={path} onNavigate={navigate}>
      {path === "/screener" ? <Screener onOpen={(ticker) => navigate(`/symbols/${ticker}`)} /> : null}
      {path === "/rules" ? <Rules /> : null}
      {symbol ? <SymbolPage symbol={symbol} /> : null}
      {path !== "/screener" && path !== "/rules" && !symbol ? <Dashboard onOpen={(ticker) => navigate(`/symbols/${ticker}`)} /> : null}
    </Layout>
  );
}

function symbolFromPath(path: string): string | null {
  const match = /^\/symbols\/([A-Za-z0-9.-]{1,16})\/?$/.exec(path);
  if (!match) return null;
  return match[1].toUpperCase();
}
