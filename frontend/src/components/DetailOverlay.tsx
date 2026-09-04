"use client";

import { ReactNode, useEffect } from "react";

interface DetailOverlayProps {
  title: string;
  open: boolean;
  onClose: () => void;
  children: ReactNode;
}

export function DetailOverlay({ title, open, onClose, children }: DetailOverlayProps) {
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape" && open) {
        onClose();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div 
      className="fixed inset-0 z-[100] flex items-center justify-center p-4 sm:p-8 lg:p-16 bg-black/60 backdrop-blur-md animate-in fade-in duration-200"
      onClick={onClose}
    >
      <div 
        className="relative w-full max-w-[1400px] h-full max-h-[90vh] flex flex-col animate-in zoom-in-95 duration-200"
        onClick={(e) => e.stopPropagation()}
      >
        <button
          onClick={onClose}
          className="absolute -top-12 right-0 flex h-10 w-10 items-center justify-center rounded-full bg-glass-panel border border-glass-border shadow-lg text-text-mute transition-colors hover:text-text z-50 hover:bg-glass-raised"
          aria-label="Close detail view"
        >
          <svg viewBox="0 0 24 24" width="20" height="20" stroke="currentColor" strokeWidth="2" fill="none" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>

        <div className="flex-1 h-full w-full [&>section]:h-full">
          {children}
        </div>
      </div>
    </div>
  );
}
