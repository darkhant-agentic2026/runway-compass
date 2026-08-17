/**
 * Attaching a file, and — the part that actually bit — what happens when it fails.
 *
 * A `POST /api/uploads` that 500s produced *no visible change at all* in a deployed
 * environment: no chip, no error, nothing. Two causes, and each hid the other. The
 * handler reported through `toast.error` while no `<Toaster />` was mounted anywhere in
 * the tree, so every message was a silent no-op; and a failure left no trace in the
 * store either, so there was nothing on screen to explain.
 *
 * These tests pin the reporting rather than the rendering: that a failure is announced,
 * and that it leaves no phantom attachment behind. `App.test.tsx` covers the other half —
 * that something is mounted to receive the announcement.
 */

import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useAttachmentUploads } from '@/features/use-uploads'
import { ApiError, api } from '@/lib/api'
import { useComposerStore } from '@/stores/composer'

const SESSION = 's_1'

vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }))
const { toast } = await import('sonner')

function png(name = 'shot.png', size = 1024): File {
  return new File([new Uint8Array(size)], name, { type: 'image/png' })
}

function attachments() {
  return useComposerStore.getState().attachments[SESSION] ?? []
}

afterEach(() => {
  vi.restoreAllMocks()
  useComposerStore.setState({ drafts: {}, attachments: {} })
})

describe('a successful upload', () => {
  it('shows the attachment while it uploads, then marks it ready', async () => {
    vi.spyOn(api, 'createUpload').mockResolvedValue({
      uploadId: 'up_1',
      signedUrl: 'https://storage.example/put',
    })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200 }))
    vi.spyOn(api, 'finalizeUpload').mockResolvedValue({
      uploadId: 'up_1',
      mimeType: 'image/png',
    })

    const { result } = renderHook(() => useAttachmentUploads(SESSION))
    await act(async () => {
      await result.current.upload(png())
    })

    await waitFor(() => expect(attachments()).toHaveLength(1))
    // `ready` is what the composer gates sending on: an attachment the server has not
    // verified must not be attachable.
    expect(attachments()[0]).toMatchObject({ uploadId: 'up_1', ready: true })
  })

  it('PUTs the bytes to the signed URL with the declared content type', async () => {
    vi.spyOn(api, 'createUpload').mockResolvedValue({
      uploadId: 'up_1',
      signedUrl: 'https://storage.example/put',
    })
    const fetchSpy = vi.fn().mockResolvedValue({ ok: true, status: 200 })
    vi.stubGlobal('fetch', fetchSpy)
    vi.spyOn(api, 'finalizeUpload').mockResolvedValue({
      uploadId: 'up_1',
      mimeType: 'image/png',
    })

    const { result } = renderHook(() => useAttachmentUploads(SESSION))
    await act(async () => {
      await result.current.upload(png())
    })

    expect(fetchSpy).toHaveBeenCalledWith(
      'https://storage.example/put',
      expect.objectContaining({
        method: 'PUT',
        headers: { 'Content-Type': 'image/png' },
      }),
    )
  })
})

describe('a failing upload', () => {
  it('reports the server’s reason rather than failing silently', async () => {
    vi.spyOn(api, 'createUpload').mockRejectedValue(
      new ApiError(500, {
        type: '/problems/internal-error',
        title: 'Internal Server Error',
        status: 500,
        detail: 'Cannot sign an upload URL.',
      }),
    )

    const { result } = renderHook(() => useAttachmentUploads(SESSION))
    await act(async () => {
      await result.current.upload(png())
    })

    expect(toast.error).toHaveBeenCalledWith(
      expect.stringContaining('Cannot sign an upload URL.'),
    )
  })

  it('leaves no attachment behind when the signed URL cannot be obtained', async () => {
    vi.spyOn(api, 'createUpload').mockRejectedValue(new Error('boom'))

    const { result } = renderHook(() => useAttachmentUploads(SESSION))
    await act(async () => {
      await result.current.upload(png())
    })

    expect(attachments()).toHaveLength(0)
  })

  it('removes the pending chip when the PUT itself is rejected', async () => {
    // The composer would otherwise look armed with an attachment the server never saw.
    vi.spyOn(api, 'createUpload').mockResolvedValue({
      uploadId: 'up_1',
      signedUrl: 'https://storage.example/put',
    })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 403 }))

    const { result } = renderHook(() => useAttachmentUploads(SESSION))
    await act(async () => {
      await result.current.upload(png())
    })

    expect(attachments()).toHaveLength(0)
    expect(toast.error).toHaveBeenCalledWith(expect.stringContaining('403'))
  })

  it('removes the pending chip when finalize refuses what landed', async () => {
    vi.spyOn(api, 'createUpload').mockResolvedValue({
      uploadId: 'up_1',
      signedUrl: 'https://storage.example/put',
    })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200 }))
    vi.spyOn(api, 'finalizeUpload').mockRejectedValue(
      new ApiError(422, {
        type: '/problems/validation-error',
        title: 'Unprocessable entity',
        status: 422,
        detail: 'The uploaded object is application/x-msdownload.',
      }),
    )

    const { result } = renderHook(() => useAttachmentUploads(SESSION))
    await act(async () => {
      await result.current.upload(png())
    })

    expect(attachments()).toHaveLength(0)
  })
})

describe('client-side refusals', () => {
  it('refuses a type the contract does not accept, without calling the server', async () => {
    const spy = vi.spyOn(api, 'createUpload')

    const { result } = renderHook(() => useAttachmentUploads(SESSION))
    await act(async () => {
      await result.current.upload(new File(['x'], 'a.zip', { type: 'application/zip' }))
    })

    expect(spy).not.toHaveBeenCalled()
    expect(toast.error).toHaveBeenCalledWith(expect.stringContaining('a.zip'))
  })

  it('refuses a file over the 20 MB cap', async () => {
    const spy = vi.spyOn(api, 'createUpload')
    const huge = png('huge.png', 1)
    Object.defineProperty(huge, 'size', { value: 21 * 1024 * 1024 })

    const { result } = renderHook(() => useAttachmentUploads(SESSION))
    await act(async () => {
      await result.current.upload(huge)
    })

    expect(spy).not.toHaveBeenCalled()
    expect(toast.error).toHaveBeenCalledWith(expect.stringContaining('20 MB'))
  })
})
