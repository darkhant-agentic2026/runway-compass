/**
 * Sign in — `/login`.
 *
 * Google sign-in through Identity Platform's popup in deployed environments. Under
 * `VITE_AUTH_MODE=dev` the same button mints a `dev:<uid>` token instead, which is what
 * the local loop and the Playwright fixture use — and which the server accepts only when
 * `ENV=local` (docs/04-api-contract.md#authentication).
 *
 * The login screen needs a theme too, which is why the theme is read from `localStorage`
 * before auth resolves rather than from `globalPrefs`.
 */

import { useEffect } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useAuth } from '@/features/use-auth'

export default function LoginPage() {
  const auth = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const from = (location.state as { from?: string } | null)?.from ?? '/'

  useEffect(() => {
    if (auth.status === 'signed-in') void navigate(from, { replace: true })
  }, [auth.status, from, navigate])

  if (auth.status === 'signed-in') return <Navigate to={from} replace />

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>Self-Study Coach</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-muted-foreground text-sm">
            Turn a technical goal into bite-sized tasks, with a coach that prepares the
            material before you sit down.
          </p>
          <Button
            className="w-full"
            disabled={auth.status === 'resolving'}
            onClick={() => void auth.signIn()}
          >
            {auth.mode === 'dev' ? 'Continue as the local dev user' : 'Sign in with Google'}
          </Button>
          {auth.mode === 'dev' ? (
            <p className="text-muted-foreground text-xs">
              Local development: this signs in with a <code>dev:</code> token, which the API
              accepts only when <code>ENV=local</code>.
            </p>
          ) : null}
        </CardContent>
      </Card>
    </div>
  )
}
