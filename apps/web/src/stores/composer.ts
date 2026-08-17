/**
 * `useComposerStore` — "draft text and pending attachments per session (survives
 * navigation)". docs/06-frontend.md#zustand-client-only-state.
 *
 * Per session rather than global: a half-written message about one task is not a draft
 * for another, and navigating between two open workspaces should not swap them.
 *
 * Not persisted to `localStorage`. A draft is scratch for this tab and this visit; the
 * board filters are persisted because they are a preference, and a half-typed sentence
 * resurfacing days later is a surprise rather than a convenience.
 */

import { create } from 'zustand'

/**
 * The empty result every "no attachments yet" read shares.
 *
 * Zustand compares a selector's result with `Object.is` to decide whether to re-render.
 * A selector ending in `?? []` therefore returns a *different* empty array on every call
 * and re-renders forever — React reports it as error #185, "maximum update depth
 * exceeded", which names the symptom and not the cause. One frozen constant fixes it, and
 * freezing it means a caller that tries to mutate the shared empty fails loudly instead
 * of corrupting every other component's idea of "empty".
 */
export const NO_ATTACHMENTS: readonly PendingAttachment[] = Object.freeze([])

export interface PendingAttachment {
  uploadId: string
  filename: string
  mimeType: string
  /** `false` until `POST /api/uploads/{id}/finalize` succeeds; not sendable before then. */
  ready: boolean
}

interface ComposerStore {
  drafts: Record<string, string>
  attachments: Record<string, PendingAttachment[]>
  draftFor: (sessionId: string) => string
  setDraft: (sessionId: string, text: string) => void
  /** Readonly, and stable when empty, so it is safe to call from a selector. */
  attachmentsFor: (sessionId: string) => readonly PendingAttachment[]
  addAttachment: (sessionId: string, attachment: PendingAttachment) => void
  markReady: (sessionId: string, uploadId: string, mimeType: string) => void
  removeAttachment: (sessionId: string, uploadId: string) => void
  reset: (sessionId: string) => void
}

export const useComposerStore = create<ComposerStore>((set, get) => ({
  drafts: {},
  attachments: {},

  draftFor: (sessionId) => get().drafts[sessionId] ?? '',

  setDraft(sessionId, text) {
    set((state) => ({ drafts: { ...state.drafts, [sessionId]: text } }))
  },

  attachmentsFor: (sessionId) => get().attachments[sessionId] ?? NO_ATTACHMENTS,

  addAttachment(sessionId, attachment) {
    set((state) => ({
      attachments: {
        ...state.attachments,
        [sessionId]: [...(state.attachments[sessionId] ?? []), attachment],
      },
    }))
  },

  markReady(sessionId, uploadId, mimeType) {
    set((state) => ({
      attachments: {
        ...state.attachments,
        [sessionId]: (state.attachments[sessionId] ?? []).map((attachment) =>
          attachment.uploadId === uploadId
            ? { ...attachment, ready: true, mimeType }
            : attachment,
        ),
      },
    }))
  },

  removeAttachment(sessionId, uploadId) {
    set((state) => ({
      attachments: {
        ...state.attachments,
        [sessionId]: (state.attachments[sessionId] ?? []).filter(
          (attachment) => attachment.uploadId !== uploadId,
        ),
      },
    }))
  },

  reset(sessionId) {
    set((state) => ({
      drafts: { ...state.drafts, [sessionId]: '' },
      attachments: { ...state.attachments, [sessionId]: [] },
    }))
  },
}))
