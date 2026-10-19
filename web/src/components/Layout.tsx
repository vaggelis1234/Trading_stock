import type { ReactNode } from "react";

export type AppPath = "/" | "/screener" | "/rules" | `/symbols/${string}`;

interface LayoutProps {
  path: string;
  onNavigate: (path: string) => void;
  children: ReactNode;
}

export function Layout({ path, onNavigate, children }: LayoutProps) {
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">UN</span>
          <div>
            <strong>Universe</strong>
            <small>Read only</small>
          </div>
        </div>
        <nav className="nav">
          <NavLink href="/" active={path === "/"} onNavigate={onNavigate}>
            Dashboard
          </NavLink>
          <NavLink href="/screener" active={path === "/screener"} onNavigate={onNavigate}>
            Screener
          </NavLink>
          <NavLink href="/rules" active={path === "/rules"} onNavigate={onNavigate}>
            Rules
          </NavLink>
        </nav>
        <p className="sidebar-note">Αναλυτικό scan. Το rank δεν είναι εντολή.</p>
      </aside>
      <main className="main">{children}</main>
    </div>
  );
}

function NavLink({
  href,
  active,
  onNavigate,
  children,
}: {
  href: string;
  active: boolean;
  onNavigate: (path: string) => void;
  children: string;
}) {
  return (
    <a
      href={href}
      className={active ? "nav-link active" : "nav-link"}
      aria-current={active ? "page" : undefined}
      onClick={(event) => {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
        event.preventDefault();
        onNavigate(href);
      }}
    >
      {children}
    </a>
  );
}
