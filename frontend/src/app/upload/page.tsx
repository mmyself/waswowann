import { Suspense } from "react";
import UploadWorkspace from "@/components/UploadWorkspace";

export default function UploadPage() {
  return <Suspense fallback={<main className="app-shell"><div className="initial-loading">Upload wird geöffnet …</div></main>}><UploadWorkspace /></Suspense>;
}
