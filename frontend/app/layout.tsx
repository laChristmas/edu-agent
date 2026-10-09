import type { ReactNode } from "react";
import "./globals.css";

export const metadata = {
  title: "EduAgent",
  description: "一个对话里的家教",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
