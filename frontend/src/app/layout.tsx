import type { Metadata, Viewport } from "next";
import "maplibre-gl/dist/maplibre-gl.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Wann Wo Was — Dein Beobachtungsarchiv",
  description: "Ein persönliches Archiv für Beobachtungen mit Ort, Zeit und Bild.",
};

export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: "#f2efe7" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="de"><body>{children}</body></html>;
}
