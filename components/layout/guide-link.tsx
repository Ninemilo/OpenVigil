import type { ComponentPropsWithoutRef } from "react";

export function GuideLink({
  children,
  ...props
}: ComponentPropsWithoutRef<"a"> & { href: string }) {
  // Guide navigation uses document requests so it works in the Worker build independently of the client router.
  return <a {...props}>{children}</a>;
}
