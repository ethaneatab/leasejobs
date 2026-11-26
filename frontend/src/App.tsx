import { useEffect, useState } from "react";
import Dashboard from "./pages/Dashboard";
import Learn from "./pages/Learn";
import Interview from "./pages/Interview";
import Problems from "./pages/Problems";

const TABS = [
  { path: "/", label: "控制台" },
  { path: "/learn", label: "教学模式" },
  { path: "/learn/interview", label: "面试题" },
  { path: "/learn/problems", label: "日常问题" },
];

export function navigate(path: string) {
  history.pushState(null, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export default function App() {
  const [path, setPath] = useState(location.pathname);
  useEffect(() => {
    const on = () => setPath(location.pathname);
    window.addEventListener("popstate", on);
    return () => window.removeEventListener("popstate", on);
  }, []);
  const page = path.startsWith("/learn/interview") ? <Interview />
    : path.startsWith("/learn/problems") ? <Problems />
    : path.startsWith("/learn") ? <Learn /> : <Dashboard />;
  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark">⧗</span> leasejobs</div>
        <nav>
          {TABS.map((t) => (
            <a key={t.path} href={t.path} data-tab={t.path}
               className={(t.path === "/learn" ? path === "/learn" : path === t.path) ? "tab on" : "tab"}
               onClick={(e) => { e.preventDefault(); navigate(t.path); }}>{t.label}</a>
          ))}
        </nav>
      </header>
      <main>{page}</main>
    </div>
  );
}
