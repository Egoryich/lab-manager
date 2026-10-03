import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, unwrap } from '@lab/client-sdk';
import type { components } from '@lab/shared-types';

type Environment = components['schemas']['EnvironmentView'];
const terminal = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED']);
const states: Record<string, string> = {
  QUEUED: 'В очереди',
  RUNNING: 'Проверяется',
  SUCCEEDED: 'Проверка завершена',
  FAILED: 'Проверка не выполнена',
  WAITING_NODE: 'Ожидание сервера',
  WAITING_RECONCILIATION: 'Требуется сверка',
  CANCEL_REQUESTED: 'Отмена запрошена',
  CANCELLED: 'Отменено',
};
const errors: Record<string, string> = {
  PROFILE_FORBIDDEN:
    'Профиль или сетевые настройки больше не разрешены. Обратитесь к администратору.',
  FORBIDDEN: 'Права преподавателя изменились.',
  GROUP_ARCHIVED: 'Группа закрыта.',
  VERSION_CONFLICT: 'Конфигурация изменилась. Обновите страницу.',
  WORKER_RETRY_REQUIRED: 'Обработчик повторит проверку автоматически.',
  WORKER_RETRY_EXHAUSTED: 'Проверка прерывалась несколько раз. Обратитесь к администратору.',
};
const capacityReasons: Record<string, string> = {
  NODE_POLICY_MISSING: 'Администратор ещё не выбрал учебное хранилище и резервы сервера.',
  NODE_NOT_ADMISSION_READY: 'Сеть и учёт машин на сервере ещё не прошли проверку.',
  INVENTORY_MISSING: 'Нет свежего снимка сервера.',
  INVENTORY_INCOMPLETE: 'В снимке сервера не хватает данных для расчёта.',
  INVENTORY_STALE: 'Снимок сервера устарел.',
  GUEST_OWNERSHIP_UNKNOWN: 'Не завершена сверка принадлежности машин.',
  GROUP_EMPTY: 'В группе пока нет студентов.',
  GROUP_ARCHIVED: 'Группа закрыта.',
  PROFILE_FORBIDDEN: 'Профиль больше не разрешён политикой преподавателя.',
  QUOTA_EXCEEDED: 'Размер группы превышает лимиты преподавателя.',
  ENVIRONMENT_ALREADY_BOOKED: 'Для этого окружения уже есть занятие в выбранное время.',
  ENVIRONMENT_BOUND_TO_OTHER_NODE: 'Диски окружения закреплены за другим сервером.',
  RAM_INSUFFICIENT: 'Недостаточно оперативной памяти.',
  CPU_INSUFFICIENT: 'Недостаточно вычислительного ресурса.',
  STORAGE_INSUFFICIENT: 'Недостаточно места на учебном хранилище.',
};

export function EnvironmentValidation({ environment }: { environment: Environment }) {
  const cache = useQueryClient();
  const key = ['operations', environment.id];
  const [requestId, setRequestId] = useState(crypto.randomUUID());
  const operations = useQuery({
    queryKey: key,
    queryFn: async () =>
      unwrap(
        await api.GET('/api/operations', {
          params: { query: { environment_id: environment.id, limit: 1 } },
        }),
      ),
    refetchInterval: (query) => {
      const operation = query.state.data?.[0];
      return operation && !terminal.has(operation.state) ? 2000 : false;
    },
  });
  const validate = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST('/api/environments/{environment_id}/validate', {
          params: { path: { environment_id: environment.id } },
          body: { request_id: requestId, expected_version: environment.version },
        }),
      ),
    onSuccess: (operation) => {
      cache.setQueryData(key, [operation]);
      setRequestId(crypto.randomUUID());
    },
    onError: () => {
      cache.invalidateQueries({ queryKey: key });
    },
  });
  const preview = useMutation({
    mutationFn: async (form: FormData) => {
      const start = new Date(String(form.get('starts_at')));
      const end = new Date(String(form.get('ends_at')));
      return unwrap(
        await api.GET('/api/environments/{environment_id}/lesson-preview', {
          params: {
            path: { environment_id: environment.id },
            query: { starts_at: start.toISOString(), ends_at: end.toISOString() },
          },
        }),
      );
    },
  });
  const operation = operations.data?.[0];
  const pending = operation && !terminal.has(operation.state);
  const estimate = operation?.result?.estimate;
  return (
    <div className="environment-validation">
      <button
        type="button"
        disabled={validate.isPending || operations.isPending || !!pending}
        onClick={() => validate.mutate()}
      >
        Проверить конфигурацию
      </button>
      {(validate.error || operations.error) && (
        <p className="error" role="alert">
          {(validate.error || operations.error)?.message}
        </p>
      )}
      {operation && (
        <div role="status">
          <p>{states[operation.state] ?? operation.state}</p>
          {operation.state === 'QUEUED' && <small>Запрос сохранён. Ожидаем обработчик.</small>}
          {operation.error_code && (
            <p className="error">
              {errors[operation.error_code] ?? 'Не удалось выполнить проверку. Повторите позже.'}
            </p>
          )}
          {estimate && (
            <>
              <p className={estimate.within_per_environment_limits ? 'success' : 'error'}>
                {estimate.within_per_environment_limits
                  ? 'Конфигурация соответствует текущим правам и лимитам одного окружения.'
                  : 'Текущий состав группы превышает лимиты окружения.'}
              </p>
              <small>
                {estimate.student_count} студентов и демо · {estimate.total.memory_mib} MiB RAM ·{' '}
                {(estimate.total.disk_bytes / 2 ** 30).toFixed(1)} GiB дисков
              </small>
              <small>
                Проверено: {new Date(operation.result!.checked_at).toLocaleString('ru-RU')}.
                Свободные ресурсы Proxmox ещё не проверены. Бронь не создана.
              </small>
            </>
          )}
        </div>
      )}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          preview.mutate(new FormData(event.currentTarget));
        }}
      >
        <div className="field-grid">
          <label>
            Начало занятия
            <input name="starts_at" type="datetime-local" required />
          </label>
          <label>
            Конец занятия
            <input name="ends_at" type="datetime-local" required />
          </label>
        </div>
        <button type="submit" disabled={preview.isPending}>
          Проверить доступность сервера
        </button>
      </form>
      {preview.error && (
        <p role="alert" className="error">
          {preview.error.message}
        </p>
      )}
      {preview.data && (
        <div role="status">
          <p>
            Предварительный расчёт: {preview.data.student_count} студентов и демо,{' '}
            {preview.data.total.memory_mib} MiB RAM,{' '}
            {(preview.data.total.disk_bytes / 2 ** 30).toFixed(1)} GiB дисков.
          </p>
          {preview.data.nodes.length === 0 && <p>Учебный сервер ещё не настроен.</p>}
          {preview.data.reasons.length > 0 && (
            <ul>
              {preview.data.reasons.map((reason) => (
                <li key={reason}>{capacityReasons[reason] ?? reason}</li>
              ))}
            </ul>
          )}
          {preview.data.nodes.map((node) => (
            <div key={node.node_id}>
              <strong>
                {node.node_name}: {node.available ? 'ресурсы доступны' : 'запуск пока невозможен'}
              </strong>
              {node.reasons.length > 0 && (
                <ul>
                  {node.reasons.map((reason) => (
                    <li key={reason}>{capacityReasons[reason] ?? reason}</li>
                  ))}
                </ul>
              )}
            </div>
          ))}
          <small>
            Это снимок доступности, бронь не создана. При бронировании расчёт повторится.
          </small>
        </div>
      )}
    </div>
  );
}
