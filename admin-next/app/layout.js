import "./globals.css";

export const metadata = {
  title: "Muse Admin",
  description: "Dashboard admin Muse bridge",
};

export default function RootLayout({ children }) {
  return (
    <html lang="id">
      <body>{children}</body>
    </html>
  );
}
