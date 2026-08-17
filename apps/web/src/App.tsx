/**
 * Routes.
 *
 * docs/06-frontend.md#routes. Screens are lazy-loaded per route; the task workspace
 * (`/projects/:projectId/tasks/:taskId`) arrives with sessions and streaming at M2.
 */

import { QueryClientProvider } from '@tanstack/react-query'
import { Suspense, lazy, useEffect } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'

import { AppShell } from '@/components/layout/AppShell'
import { AuthProvider, RequireAuth } from '@/features/auth-context'
import { createQueryClient } from '@/features/queries'
import { startThemeSync } from '@/stores/theme'

const LoginPage = lazy(() => import('@/pages/LoginPage'))
const ProjectsPage = lazy(() => import('@/pages/ProjectsPage'))
const BoardPage = lazy(() => import('@/pages/BoardPage'))
const ProjectSettingsPage = lazy(() => import('@/pages/ProjectSettingsPage'))
const SettingsPage = lazy(() => import('@/pages/SettingsPage'))

const queryClient = createQueryClient()

export default function App() {
  useEffect(() => startThemeSync(), [])

  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <Suspense fallback={<div className="h-full" aria-busy="true" />}>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route
              element={
                <RequireAuth>
                  <AppShell />
                </RequireAuth>
              }
            >
              <Route path="/" element={<ProjectsPage />} />
              <Route path="/projects/:projectId" element={<BoardPage />} />
              <Route path="/projects/:projectId/settings" element={<ProjectSettingsPage />} />
              <Route path="/settings" element={<SettingsPage />} />
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Suspense>
      </AuthProvider>
    </QueryClientProvider>
  )
}
