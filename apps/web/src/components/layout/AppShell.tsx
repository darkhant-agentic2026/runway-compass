import { Link, Outlet } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { useAuth } from '@/features/use-auth'

export function AppShell() {
  const auth = useAuth()

  return (
    <div className="flex min-h-full flex-col">
      <header className="border-b">
        <nav className="mx-auto flex w-full max-w-3xl items-center gap-3 p-3 sm:px-6">
          <Link to="/" className="font-semibold">
            Self-Study Coach
          </Link>
          <span className="flex-1" />
          <Button variant="ghost" render={<Link to="/settings" />}>
            Settings
          </Button>
          <Button variant="ghost" onClick={() => void auth.signOut()}>
            Sign out
          </Button>
        </nav>
      </header>
      <main className="flex-1">
        <Outlet />
      </main>
    </div>
  )
}
