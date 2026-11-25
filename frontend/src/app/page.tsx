import { Suspense } from "react";
import Workspace from "@/components/Workspace";

export default function HomePage() {
  return <Suspense fallback={<main className="app-shell"><div className="initial-loading">Archiv wird geöffnet …</div></main>}><Workspace /></Suspense>;
}
