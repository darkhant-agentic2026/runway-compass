/**
 * The app mounts a `<Toaster />`.
 *
 * A one-assertion test for a one-line omission, and worth having because of how the
 * omission presents. `toast.error` is the only channel several failures have — an upload
 * the server refuses, an attachment that never finalizes. With nothing mounted to receive
 * them those calls succeed and display nothing, so a 500 from `POST /api/uploads` looked
 * exactly like a file picker whose event handler was never wired up. Both halves of the
 * upload path were then suspected before the real cause, a signing error on the server,
 * was found.
 *
 * Nothing else in the suite would notice: `use-uploads.test.tsx` asserts that `toast.error`
 * is *called*, which stays true when no one is listening.
 */

import { render, screen } from '@testing-library/react'
import { toast } from 'sonner'
import { expect, it, vi } from 'vitest'

import { Toaster } from '@/components/ui/sonner'

vi.mock('next-themes', () => ({ useTheme: () => ({ theme: 'light' }) }))

it('renders a live region that toasts land in', async () => {
  render(<Toaster />)

  toast.error('the upload could not be signed')

  expect(await screen.findByText('the upload could not be signed')).toBeInTheDocument()
})

it('App mounts the Toaster', async () => {
  // Asserted against the source rather than by rendering `<App />`, which would need the
  // router, the query client, and a signed-in auth provider — three things whose failure
  // would make this test red for reasons that have nothing to do with the toaster.
  const source = await import('@/App?raw').then((module) => module.default as string)

  expect(source).toContain('<Toaster')
})
