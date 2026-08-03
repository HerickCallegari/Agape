import { serve } from "https://deno.land/std@0.224.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  if (req.method !== "POST") {
    return json({ error: "Método não permitido." }, 405);
  }

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const anonKey = Deno.env.get("SUPABASE_ANON_KEY");
  const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");

  if (!supabaseUrl || !anonKey || !serviceRoleKey) {
    return json({ error: "Função sem configuração do Supabase." }, 500);
  }

  const authHeader = req.headers.get("Authorization") ?? "";
  const authClient = createClient(supabaseUrl, anonKey, {
    global: { headers: { Authorization: authHeader } },
  });
  const adminClient = createClient(supabaseUrl, serviceRoleKey);

  const { data: userData, error: userError } = await authClient.auth.getUser();
  if (userError || !userData.user) {
    return json({ error: "Sessão inválida." }, 401);
  }

  const { data: callerProfile, error: profileError } = await adminClient
    .from("profiles")
    .select("role, is_active")
    .eq("auth_user_id", userData.user.id)
    .single();

  if (profileError || !callerProfile?.is_active) {
    return json({ error: "Perfil sem permissão." }, 403);
  }

  if (!["admin", "reception"].includes(callerProfile.role)) {
    return json({ error: "Apenas administradores e recepcao podem trocar senhas." }, 403);
  }

  const body = await req.json().catch(() => ({}));
  const authUserId = String(body.auth_user_id ?? "");
  const password = String(body.password ?? "");

  if (!authUserId) {
    return json({ error: "Usuário alvo não informado." }, 400);
  }

  if (password.length < 6) {
    return json({ error: "A senha deve ter pelo menos 6 caracteres." }, 400);
  }

  const { data: targetUser, error: getTargetError } = await adminClient.auth.admin.getUserById(authUserId);
  if (getTargetError || !targetUser.user) {
    return json({ error: "Usuário alvo não encontrado no Auth." }, 404);
  }

  const { data: updatedUser, error } = await adminClient.auth.admin.updateUserById(authUserId, { password });
  if (error) {
    return json({ error: error.message }, 400);
  }

  return json({ ok: true, user_id: updatedUser.user?.id, email: updatedUser.user?.email });
});

function json(data: Record<string, unknown>, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}
