"use client";

import { useEffect } from "react";

import {
  getLocalLibraryState,
  renewPortableCacheLease,
} from "./localPaperLibrary";

const LEASE_RENEW_INTERVAL_MS = 60_000;

export function usePortableCacheLease(paperId: string, active = true): void {
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    let timer: number | null = null;
    void getLocalLibraryState().then((state) => {
      if (
        cancelled ||
        state.mode !== "local_folder" ||
        state.permission !== "granted"
      ) {
        return;
      }
      const renew = () => {
        void renewPortableCacheLease(paperId);
      };
      renew();
      timer = window.setInterval(renew, LEASE_RENEW_INTERVAL_MS);
    });
    return () => {
      cancelled = true;
      if (timer !== null) window.clearInterval(timer);
    };
  }, [active, paperId]);
}
