import React, { useState, useMemo } from "react";
import { motion, useMotionValue, useTransform, AnimatePresence } from "framer-motion";
import { Eye, EyeOff, Loader2, Shield, Github, Mail } from "lucide-react";

export default function Login({
  onSubmit,
  loading = false,
  errorMessage = "",
}) {
  const [form, setForm] = useState({ username: "", password: "", remember: false });
  const [touched, setTouched] = useState({ username: false, password: false });
  const [showPassword, setShowPassword] = useState(false);

  // Password strength (simple evaluation)
  const strength = useMemo(() => {
    if (!form.password) return { label: "", value: 0, color: "bg-transparent" };
    const score =
      (/[a-z]/.test(form.password) ? 1 : 0) +
      (/[A-Z]/.test(form.password) ? 1 : 0) +
      (/\d/.test(form.password) ? 1 : 0) +
      (form.password.length >= 8 ? 1 : 0);
    if (score <= 1) return { label: "Débil", value: 25, color: "bg-red-500" };
    if (score === 2) return { label: "Media", value: 50, color: "bg-yellow-500" };
    if (score === 3) return { label: "Buena", value: 75, color: "bg-cyan-400" };
    return { label: "Fuerte", value: 100, color: "bg-emerald-500" };
  }, [form.password]);

  // Handlers
  const handleChange = (e) =>
    setForm({ ...form, [e.target.name]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const handleBlur = (e) => setTouched({ ...touched, [e.target.name]: true });
  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!form.username || !form.password) {
      setTouched({ username: true, password: true });
      return;
    }
    if (onSubmit) await onSubmit({ username: form.username, password: form.password, remember: form.remember });
  };

  // Card parallax effect
  const x = useMotionValue(0);
  const y = useMotionValue(0);
  const rotateX = useTransform(y, [-50, 50], [8, -8]);
  const rotateY = useTransform(x, [-50, 50], [-8, 8]);

  const handleMouseMove = (e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    x.set(e.clientX - rect.left - rect.width / 2);
    y.set(e.clientY - rect.top - rect.height / 2);
  };
  const handleMouseLeave = () => {
    x.set(0);
    y.set(0);
  };

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-[#0b1220] text-gray-100">
      {/* Glow orbs */}
      <div className="pointer-events-none absolute inset-0">
        <div className="absolute -left-20 top-1/4 h-72 w-72 rounded-full bg-cyan-500/20 blur-3xl" />
        <div className="absolute right-0 top-0 h-96 w-96 rounded-full bg-blue-500/20 blur-3xl" />
        <div className="absolute bottom-0 left-1/3 h-64 w-64 rounded-full bg-cyan-400/10 blur-2xl" />
      </div>

      {/* Subtle grid + noise */}
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_at_top,_var(--tw-gradient-stops))] from-[#0f172a] via-[#0b1220] to-[#0b1220]">
        <div className="absolute inset-0 bg-[url('data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 10 10%22%3E%3Cpath fill=%22%23111827%22 d=%22M10 0H0V10H10V0Z%22/%3E%3C/svg%3E')] opacity-30 mix-blend-overlay" />
        <div className="absolute inset-0 bg-[url('data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%22100%22 height=%22100%22%3E%3Cfilter id=%22noise%22%3E%3CfeTurbulence type=%22fractalNoise%22 baseFrequency=%220.65%22%3E%3C/feTurbulence%3E%3C/filter%3E%3Crect width=%22100%25%22 height=%22100%25%22 filter=%22url(%23noise)%22 opacity=%220.05%22/%3E%3C/svg%3E')]" />
      </div>

      {/* Login card */}
      <motion.div
        className="relative w-full max-w-sm"
        initial={{ opacity: 0, scale: 0.9 }}
        animate={{ opacity: 1, scale: 1 }}
      >
        <motion.div
          onMouseMove={handleMouseMove}
          onMouseLeave={handleMouseLeave}
          style={{ rotateX, rotateY }}
          className="relative rounded-2xl bg-white/5 p-8 backdrop-blur-xl shadow-2xl shadow-cyan-500/10"
        >
          {/* Header */}
          <div className="mb-6 flex flex-col items-center gap-2">
            <Shield className="h-10 w-10 text-cyan-400" aria-hidden="true" />
            <h1 className="text-xl font-semibold tracking-wide">Iniciar sesión</h1>
          </div>

          {/* Error banner */}
          <AnimatePresence>
            {errorMessage && (
              <motion.div
                data-testid="error-banner"
                className="mb-4 rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300"
                role="alert"
                initial={{ opacity: 0, y: -10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
              >
                {errorMessage}
              </motion.div>
            )}
          </AnimatePresence>

          <form onSubmit={handleSubmit} className="flex flex-col gap-5">
            {/* Username */}
            <div className="flex flex-col">
              <label htmlFor="username" className="mb-1 text-sm">
                Usuario
              </label>
              <input
                data-testid="username"
                id="username"
                name="username"
                type="text"
                autoComplete="username"
                value={form.username}
                onChange={handleChange}
                onBlur={handleBlur}
                disabled={loading}
                className="rounded-xl bg-white/10 px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-cyan-400 disabled:opacity-50"
                placeholder="tu_usuario"
                aria-invalid={touched.username && !form.username}
                aria-describedby={touched.username && !form.username ? "username-error" : undefined}
              />
              {touched.username && !form.username && (
                <span id="username-error" className="mt-1 text-xs text-red-400">
                  Usuario requerido
                </span>
              )}
            </div>

            {/* Password */}
            <div className="flex flex-col">
              <label htmlFor="password" className="mb-1 text-sm">
                Contraseña
              </label>
              <div className="relative">
                <input
                  data-testid="password"
                  id="password"
                  name="password"
                  type={showPassword ? "text" : "password"}
                  autoComplete="current-password"
                  value={form.password}
                  onChange={handleChange}
                  onBlur={handleBlur}
                  disabled={loading}
                  className="w-full rounded-xl bg-white/10 px-4 py-3 pr-12 text-sm focus:outline-none focus:ring-2 focus:ring-cyan-400 disabled:opacity-50"
                  placeholder="••••••••"
                  aria-invalid={touched.password && !form.password}
                  aria-describedby={touched.password && !form.password ? "password-error" : undefined}
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute inset-y-0 right-0 flex items-center pr-4 text-gray-300 hover:text-cyan-400 focus:outline-none"
                  aria-label={showPassword ? "Ocultar contraseña" : "Mostrar contraseña"}
                >
                  <motion.div
                    whileTap={{ scale: 0.9, rotate: 15 }}
                    transition={{ type: "spring", stiffness: 300 }}
                  >
                    {showPassword ? <EyeOff className="h-5 w-5" /> : <Eye className="h-5 w-5" />}
                  </motion.div>
                </button>
              </div>
              {touched.password && !form.password && (
                <span id="password-error" className="mt-1 text-xs text-red-400">
                  Contraseña requerida
                </span>
              )}

              {/* Password strength */}
              {strength.value > 0 && (
                <div className="mt-2 flex items-center gap-2 text-xs">
                  <div className="h-1 flex-1 overflow-hidden rounded bg-white/10">
                    <div
                      className={`h-full ${strength.color}`}
                      style={{ width: `${strength.value}%` }}
                    />
                  </div>
                  <span className="text-gray-400">{strength.label}</span>
                </div>
              )}
            </div>

            {/* Remember / Forgot */}
            <div className="flex items-center justify-between text-xs text-gray-400">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  name="remember"
                  checked={form.remember}
                  onChange={handleChange}
                  disabled={loading}
                  className="h-4 w-4 rounded border-gray-600 bg-transparent text-cyan-500 focus:ring-cyan-400"
                />
                Recordarme
              </label>
              <a href="#" className="hover:text-cyan-400 focus:text-cyan-400 focus:outline-none">
                ¿Olvidaste tu contraseña?
              </a>
            </div>

            {/* Submit button */}
            <motion.button
              data-testid="submit"
              type="submit"
              disabled={loading}
              className="flex items-center justify-center rounded-xl bg-cyan-500 px-4 py-3 text-sm font-medium text-[#0b1220] shadow-lg shadow-cyan-500/20 hover:bg-cyan-400 focus:outline-none focus:ring-2 focus:ring-cyan-400 disabled:opacity-50"
              whileHover={{ scale: loading ? 1 : 1.02 }}
              whileTap={{ scale: loading ? 1 : 0.98 }}
            >
              {loading ? <Loader2 className="h-5 w-5 animate-spin" /> : "Ingresar"}
            </motion.button>

            {/* Social logins (placeholders) */}
            <div className="mt-2 flex gap-3">
              <button
                type="button"
                className="flex flex-1 items-center justify-center gap-2 rounded-xl border border-gray-600 bg-white/5 p-2 text-sm text-gray-300 hover:border-cyan-400 hover:text-cyan-400 focus:outline-none focus:ring-2 focus:ring-cyan-400"
              >
                <Mail className="h-4 w-4" /> Google
              </button>
              <button
                type="button"
                className="flex flex-1 items-center justify-center gap-2 rounded-xl border border-gray-600 bg-white/5 p-2 text-sm text-gray-300 hover:border-cyan-400 hover:text-cyan-400 focus:outline-none focus:ring-2 focus:ring-cyan-400"
              >
                <Github className="h-4 w-4" /> GitHub
              </button>
            </div>
          </form>

          {/* Secondary links */}
          <div className="mt-6 text-center text-sm">
            <span className="text-gray-400">¿No tienes cuenta?</span>{" "}
            <a href="#" className="text-cyan-400 hover:underline focus:outline-none">
              Crear cuenta
            </a>
          </div>
        </motion.div>

        {/* Footer */}
        <div classabel="mt-4 flex items-center justify-center gap-2 text-xs text-gray-500">
          <motion.div
            animate={{ opacity: [0.6, 1, 0.6] }}
            transition={{ repeat: Infinity, duration: 2 }}
            className="h-2 w-2 rounded-full bg-emerald-400"
          />
          Conexión segura
        </div>
      </motion.div>
    </div>
  );
}
