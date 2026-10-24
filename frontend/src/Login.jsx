/**
 * The `/login` route.
 *
 * Wraps the existing `LoginView` (its form, validation, and status-derived
 * error copy are unchanged) and adds the one thing routing needs: on success,
 * flip the auth state and navigate to wherever the Reviewer was headed before
 * the guard bounced them here (`location.state.from`), defaulting to
 * `/dashboard`.
 *
 * The sign-in form is given the UNGUARDED client, so a wrong-password 401 is
 * an ordinary form error rather than tripping the sign-out guard.
 */

import { Link, useLocation, useNavigate } from 'react-router-dom';

import LoginView from './LoginView.jsx';
import { useAuth } from './auth.jsx';

export default function Login() {
  const { rawClient, signIn } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  // Where the guard wanted to send them; default to the dashboard.
  const from = location.state?.from?.pathname ?? '/dashboard';

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-canvas px-4 py-10">
      <LoginView
        client={rawClient}
        onSignedIn={() => {
          signIn();
          navigate(from, { replace: true });
        }}
      />
      <p className="mt-6 text-sm text-fg-muted">
        Need an account?{' '}
        <Link to="/signup" className="font-medium text-accent hover:underline">
          Sign up
        </Link>
      </p>
    </div>
  );
}
