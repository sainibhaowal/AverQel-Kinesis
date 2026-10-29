import { Metadata } from "next";
import { PRODUCT_NAME } from "@/lib/brand";

export const metadata: Metadata = {
  title: `Documentation | ${PRODUCT_NAME}`,
  description:
    "Product guides for AverQel Documents Hub, Query, DeepSpace, Qel Connect, providers, privacy, support, and account settings.",
};

export default function DocsLayout({ children }: { children: React.ReactNode }) {
  return children;
}
