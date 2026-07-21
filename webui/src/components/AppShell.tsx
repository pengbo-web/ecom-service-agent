import * as React from "react";
import { Sidebar, type View } from "@/components/Sidebar";

export function AppShell({ view, onView, onReset, children }: {
  view: View; onView: (v: View) => void; onReset: () => void; children: React.ReactNode;
}) {
  return (
    <div className="flex h-full">
      <Sidebar view={view} onView={onView} onReset={onReset} />
      <main className="flex-1 min-w-0 overflow-hidden">{children}</main>
    </div>
  );
}
