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
import { Toaster } from '@/components/ui/sonner'
import { AuthProvider, RequireAuth } from '@/features/auth-context'
import { createQueryClient } from '@/features/queries'
import { startThemeSync } from '@/stores/theme'

const LoginPage = lazy(() => import('@/pages/LoginPage'))
const ProjectsPage = lazy(() => import('@/pages/ProjectsPage'))
const BoardPage = lazy(() => import('@/pages/BoardPage'))
const TaskWorkspacePage = lazy(() => import('@/pages/TaskWorkspacePage'))
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
              <Route
                path="/projects/:projectId/tasks/:taskId"
                element={<TaskWorkspacePage />}
              />
              <Route path="/projects/:projectId/settings" element={<ProjectSettingsPage />} />
              <Route path="/settings" element={<SettingsPage />} />
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Suspense>
        {/*
          Mounted at the root, and load-bearing rather than decorative: `toast.error` is
          the *only* way several failures reach the user — an upload that the server
          refuses, an attachment that never finalizes. Without a `<Toaster />` in the tree
          those calls are silent no-ops, and the symptom is a UI where clicking does
          nothing at all, which is indistinguishable from a dead event handler. That is
          how a 500 on `POST /api/uploads` reached a deployed environment looking like a
          broken file picker.
        */}
        <Toaster position="bottom-right" closeButton />
      </AuthProvider>
    </QueryClientProvider>
  )
}
