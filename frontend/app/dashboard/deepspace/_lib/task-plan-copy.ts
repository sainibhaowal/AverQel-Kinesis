/**
 * Locale copy for the DeepSpace task-plan drawer.
 *
 * DeepSpace has no request-wide frontend locale module, so the drawer keeps
 * its own small table. The eight locales mirror the Harness Todo package so a
 * plan reads the same way in the API text and in the UI. English is the
 * fallback for an unknown tag.
 */

export const TASK_PLAN_LOCALES = [
  "en",
  "zh",
  "ja",
  "de",
  "fr",
  "es",
  "pt",
  "ru",
] as const;

export type TaskPlanLocale = (typeof TASK_PLAN_LOCALES)[number];

export type TaskPlanStatus =
  | "pending"
  | "in_progress"
  | "completed"
  | "blocked"
  | "failed";

export type TaskPlanCopy = {
  title: string;
  status: Record<TaskPlanStatus, string>;
  needsAttention: string;
  verifiedPreparing: string;
  complete: string;
  verifyingCompletion: string;
  awaitingVerification: string;
  pausedRetry: string;
  workingThroughPlan: string;
  planIncomplete: string;
  active: string;
  paused: string;
  completionAria: string;
  plannedTasksAria: string;
  verifiedPreparingFinal: string;
  verifyingEveryTask: string;
};

export const TASK_PLAN_COPY: Record<TaskPlanLocale, TaskPlanCopy> = {
  en: {
    title: "Task plan",
    status: {
      pending: "Pending",
      in_progress: "In progress",
      completed: "Complete",
      blocked: "Blocked",
      failed: "Failed",
    },
    needsAttention: "Needs attention",
    verifiedPreparing: "Verified · preparing answer",
    complete: "Complete",
    verifyingCompletion: "Verifying completion",
    awaitingVerification: "Awaiting verification",
    pausedRetry: "Paused · retry to continue",
    workingThroughPlan: "Working through plan",
    planIncomplete: "Plan incomplete",
    active: "Active",
    paused: "Paused",
    completionAria: "Task plan completion",
    plannedTasksAria: "Planned tasks",
    verifiedPreparingFinal:
      "Task list verified · preparing the final answer",
    verifyingEveryTask: "Verifying every task before the answer",
  },
  zh: {
    title: "任务计划",
    status: {
      pending: "待处理",
      in_progress: "进行中",
      completed: "已完成",
      blocked: "已阻塞",
      failed: "失败",
    },
    needsAttention: "需要关注",
    verifiedPreparing: "已验证 · 正在准备回答",
    complete: "已完成",
    verifyingCompletion: "正在验证完成情况",
    awaitingVerification: "等待验证",
    pausedRetry: "已暂停 · 重试以继续",
    workingThroughPlan: "正在按计划执行",
    planIncomplete: "计划未完成",
    active: "进行中",
    paused: "已暂停",
    completionAria: "任务计划完成度",
    plannedTasksAria: "计划任务",
    verifiedPreparingFinal: "任务列表已验证 · 正在准备最终回答",
    verifyingEveryTask: "回答前正在验证每项任务",
  },
  ja: {
    title: "タスク計画",
    status: {
      pending: "未着手",
      in_progress: "進行中",
      completed: "完了",
      blocked: "ブロック中",
      failed: "失敗",
    },
    needsAttention: "要確認",
    verifiedPreparing: "検証済み · 回答を準備中",
    complete: "完了",
    verifyingCompletion: "完了を検証中",
    awaitingVerification: "検証待ち",
    pausedRetry: "一時停止 · 再試行して続行",
    workingThroughPlan: "計画に沿って実行中",
    planIncomplete: "計画未完了",
    active: "実行中",
    paused: "一時停止",
    completionAria: "タスク計画の進捗",
    plannedTasksAria: "計画されたタスク",
    verifiedPreparingFinal: "タスク一覧を検証済み · 最終回答を準備中",
    verifyingEveryTask: "回答前にすべてのタスクを検証中",
  },
  de: {
    title: "Aufgabenplan",
    status: {
      pending: "Ausstehend",
      in_progress: "In Bearbeitung",
      completed: "Abgeschlossen",
      blocked: "Blockiert",
      failed: "Fehlgeschlagen",
    },
    needsAttention: "Aufmerksamkeit erforderlich",
    verifiedPreparing: "Verifiziert · Antwort wird vorbereitet",
    complete: "Abgeschlossen",
    verifyingCompletion: "Abschluss wird verifiziert",
    awaitingVerification: "Verifizierung ausstehend",
    pausedRetry: "Pausiert · zum Fortsetzen erneut versuchen",
    workingThroughPlan: "Plan wird abgearbeitet",
    planIncomplete: "Plan unvollständig",
    active: "Aktiv",
    paused: "Pausiert",
    completionAria: "Fortschritt des Aufgabenplans",
    plannedTasksAria: "Geplante Aufgaben",
    verifiedPreparingFinal:
      "Aufgabenliste verifiziert · endgültige Antwort wird vorbereitet",
    verifyingEveryTask: "Jede Aufgabe wird vor der Antwort verifiziert",
  },
  fr: {
    title: "Plan de tâches",
    status: {
      pending: "En attente",
      in_progress: "En cours",
      completed: "Terminée",
      blocked: "Bloquée",
      failed: "Échouée",
    },
    needsAttention: "Attention requise",
    verifiedPreparing: "Vérifié · préparation de la réponse",
    complete: "Terminé",
    verifyingCompletion: "Vérification de l'achèvement",
    awaitingVerification: "Vérification en attente",
    pausedRetry: "En pause · relancez pour continuer",
    workingThroughPlan: "Exécution du plan",
    planIncomplete: "Plan incomplet",
    active: "Actif",
    paused: "En pause",
    completionAria: "Achèvement du plan de tâches",
    plannedTasksAria: "Tâches planifiées",
    verifiedPreparingFinal:
      "Liste de tâches vérifiée · préparation de la réponse finale",
    verifyingEveryTask: "Vérification de chaque tâche avant la réponse",
  },
  es: {
    title: "Plan de tareas",
    status: {
      pending: "Pendiente",
      in_progress: "En curso",
      completed: "Completada",
      blocked: "Bloqueada",
      failed: "Fallida",
    },
    needsAttention: "Requiere atención",
    verifiedPreparing: "Verificado · preparando respuesta",
    complete: "Completado",
    verifyingCompletion: "Verificando finalización",
    awaitingVerification: "Esperando verificación",
    pausedRetry: "En pausa · reintenta para continuar",
    workingThroughPlan: "Trabajando según el plan",
    planIncomplete: "Plan incompleto",
    active: "Activo",
    paused: "En pausa",
    completionAria: "Finalización del plan de tareas",
    plannedTasksAria: "Tareas planificadas",
    verifiedPreparingFinal:
      "Lista de tareas verificada · preparando la respuesta final",
    verifyingEveryTask: "Verificando cada tarea antes de la respuesta",
  },
  pt: {
    title: "Plano de tarefas",
    status: {
      pending: "Pendente",
      in_progress: "Em andamento",
      completed: "Concluída",
      blocked: "Bloqueada",
      failed: "Falhou",
    },
    needsAttention: "Requer atenção",
    verifiedPreparing: "Verificado · preparando resposta",
    complete: "Concluído",
    verifyingCompletion: "Verificando conclusão",
    awaitingVerification: "Aguardando verificação",
    pausedRetry: "Em pausa · tente novamente para continuar",
    workingThroughPlan: "Executando o plano",
    planIncomplete: "Plano incompleto",
    active: "Ativo",
    paused: "Em pausa",
    completionAria: "Conclusão do plano de tarefas",
    plannedTasksAria: "Tarefas planejadas",
    verifiedPreparingFinal:
      "Lista de tarefas verificada · preparando a resposta final",
    verifyingEveryTask: "Verificando cada tarefa antes da resposta",
  },
  ru: {
    title: "План задач",
    status: {
      pending: "В ожидании",
      in_progress: "В работе",
      completed: "Выполнено",
      blocked: "Заблокировано",
      failed: "Ошибка",
    },
    needsAttention: "Требует внимания",
    verifiedPreparing: "Проверено · подготовка ответа",
    complete: "Завершено",
    verifyingCompletion: "Проверка завершения",
    awaitingVerification: "Ожидает проверки",
    pausedRetry: "Приостановлено · повторите, чтобы продолжить",
    workingThroughPlan: "Работа по плану",
    planIncomplete: "План не завершён",
    active: "Активно",
    paused: "Приостановлено",
    completionAria: "Завершённость плана задач",
    plannedTasksAria: "Запланированные задачи",
    verifiedPreparingFinal:
      "Список задач проверен · подготовка окончательного ответа",
    verifyingEveryTask: "Проверка каждой задачи перед ответом",
  },
};

/** Resolve a BCP 47 tag to a supported drawer locale. */
export function resolveTaskPlanLocale(value: string | null | undefined): TaskPlanLocale {
  if (!value) return "en";
  const language = value.trim().replace(/_/g, "-").toLowerCase().split("-")[0];
  return (TASK_PLAN_LOCALES as readonly string[]).includes(language)
    ? (language as TaskPlanLocale)
    : "en";
}

/** Read the browser's preferred drawer locale. */
export function browserTaskPlanLocale(): TaskPlanLocale {
  if (typeof navigator === "undefined") return "en";
  const documentLanguage =
    typeof document !== "undefined" ? document.documentElement.lang : "";
  return resolveTaskPlanLocale(documentLanguage || navigator.language);
}

/** Return the copy table for one locale tag. */
export function taskPlanCopy(locale: string | null | undefined): TaskPlanCopy {
  return TASK_PLAN_COPY[resolveTaskPlanLocale(locale)];
}
