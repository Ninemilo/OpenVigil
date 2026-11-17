import type { Metadata } from "next";
import { introduction } from "@/lib/user-guide";
import { GuideArticle } from "./guide-article";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: introduction.title,
  description: introduction.summary,
};

export default function GuideHome() {
  return <GuideArticle chapter={introduction} />;
}
