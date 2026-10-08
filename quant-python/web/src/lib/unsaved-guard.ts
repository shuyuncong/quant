"use client";

import { useEffect } from "react";

/**
 * The same-document `navigate` event Object (`window.navigation`). TypeScript's DOM lib does not
 * declare the Navigation API yet, so the shape is modeled locally and feature-detected at runtime.
 */
interface NavigationLike {
  addEventListener(type: "navigate", listener: (event: Event) => void): void;
  removeEventListener(type: "navigate", listener: (event: Event) => void): void;
}

interface NavigateLikeEvent {
  /** Same-document traversals stay cancelable only while the page holds history-action activation. */
  cancelable: boolean;
  navigationType: "push" | "reload" | "replace" | "traverse";
  destination: { url: string };
  hashChange: boolean;
  downloadRequest: string | null;
  formData: unknown;
}

function navigateEventFrom(value: unknown): NavigateLikeEvent | null {
  if (typeof value !== "object" || value === null) return null;
  if (!("cancelable" in value) || !("destination" in value) || !("hashChange" in value)) return null;
  if (typeof value.cancelable !== "boolean" || typeof value.hashChange !== "boolean") return null;
  if (typeof value.destination !== "object" || value.destination === null) return null;
  if (!("url" in value.destination) || typeof value.destination.url !== "string") return null;
  const navigationType = "navigationType" in value ? value.navigationType : "";
  const downloadRequest = "downloadRequest" in value ? value.downloadRequest : null;
  const formData = "formData" in value ? value.formData : null;
  return {
    cancelable: value.cancelable,
    navigationType: navigationType === "push" || navigationType === "reload" || navigationType === "replace" ? navigationType : "traverse",
    destination: { url: value.destination.url },
    hashChange: value.hashChange,
    downloadRequest: typeof downloadRequest === "string" ? downloadRequest : null,
    formData,
  };
}

/** Hash-only navigation on the same path/query keeps the current draft mounted. */
function isSamePage(href: string): boolean {
  const target = new URL(href, location.href);
  return (
    target.origin === location.origin &&
    target.pathname === location.pathname &&
    target.search === location.search
  );
}

/** In-app link clicks that next/link would turn into a client navigation (not new-tab/download/hash). */
function plainLeftClickAnchor(event: MouseEvent): HTMLAnchorElement | null {
  if (event.defaultPrevented) return null;
  const anchor = event.target instanceof Element ? event.target.closest("a") : null;
  if (!anchor || anchor.hasAttribute("download")) return null;
  const target = anchor.getAttribute("target");
  if (target && target !== "_self") return null;
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return null;
  const href = anchor.getAttribute("href");
  if (!href || href.startsWith("#") || isSamePage(href)) return null;
  if (new URL(anchor.href, location.href).origin !== location.origin) return null;
  return anchor;
}

/**
 * Warns before a navigation drops unsaved form state, without touching the history stack.
 *
 * Three scoped layers, none of which patch `history` or push trap entries:
 * - `beforeunload` for reloads, window/tab closes and cross-document departures.
 * - Capture-phase clicks on same-origin `<a href>` (how `next/link` navigates; the capture phase
 *   runs before React's handlers, and `defaultPrevented` makes `next/link` skip its own push).
 * - The Navigation API `navigate` event for Back/Forward. The browser reports these as
 *   `navigationType: "traverse"` and keeps them cancelable while the page is top-level and holds
 *   history-action activation — true here because editing the form is itself a user interaction.
 *   Canceled traversals never commit, so the URL and the router stay in sync. Reloads, hash changes
 *   and programmatic pushes are left alone (`beforeunload` covers the first, the rest fire the click
 *   path or a full document load), and browsers without the API fall back to the two layers above.
 */
export function useUnsavedChangesGuard(message: string, active: boolean) {
  // Listeners exist only while a draft is pending, so an idle page never intercepts navigation.
  useEffect(() => {
    if (!active) return;

    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };

    const click = (event: MouseEvent) => {
      if (!plainLeftClickAnchor(event) || window.confirm(message)) return;
      event.preventDefault();
      event.stopPropagation();
    };

    const navigate = (raw: Event) => {
      const event = navigateEventFrom(raw);
      if (!event || !event.cancelable || event.navigationType !== "traverse" || event.hashChange) return;
      if (event.downloadRequest !== null || event.formData != null) return;
      if (new URL(event.destination.url).origin !== location.origin) return;
      if (window.confirm(message)) return;
      raw.preventDefault();
    };

    window.addEventListener("beforeunload", beforeUnload);
    document.addEventListener("click", click, true);
    const navigation = (window as unknown as { navigation?: NavigationLike }).navigation;
    navigation?.addEventListener("navigate", navigate);
    return () => {
      window.removeEventListener("beforeunload", beforeUnload);
      document.removeEventListener("click", click, true);
      navigation?.removeEventListener("navigate", navigate);
    };
  }, [message, active]);
}
