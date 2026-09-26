import { useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, unwrap } from '@lab/client-sdk';
import type { components } from '@lab/shared-types';

type Policy = components['schemas']['PolicyCreate'];
type Draft = components['schemas']['EnvironmentCreate'];
type Estimate = components['schemas']['EstimateView'];
const names: Record<string, string> = {
  can_create_groups: 'Создавать группы',
  can_delete_groups: 'Удалять группы',
  can_create_lxc: 'Создавать LXC',
  can_create_qemu: 'Создавать VM',
  can_use_linux_profiles: 'Linux',
  can_use_windows_profiles: 'Windows',
  can_use_custom_profiles: 'Пользовательские профили',
  can_create_demo_vm: 'Демонстрационные VM',
  can_enable_group_network: 'Общая сеть внутри окружения',
  can_change_network_policy: 'Менять сетевую политику',
  can_allow_internet_access: 'Разрешать Интернет',
  can_delete_own_environments: 'Удалять свои окружения',
  can_archive_environments: 'Архивировать окружения',
  can_override_idle_policy: 'Настраивать простой',
  can_override_resource_limits: 'Настраивать ресурсы в разрешённых границах',
  can_power_on_node: 'Включать сервер',
  can_request_node_shutdown: 'Запрашивать выключение сервера',
  can_use_exclusive_mode: 'Эксклюзивное занятие',
};
const limitNames = {
  max_lxc_per_environment: 'LXC на окружение',
  max_vm_per_environment: 'VM на окружение',
  max_total_ram_mb: 'RAM, MiB',
  max_cpu_credits: 'CPU, кредиты',
  max_disk_gb: 'Диски и гибернация, GiB',
  max_active_environments: 'Одновременные занятия',
};
const submit = (fn: (data: FormData) => void) => (e: FormEvent<HTMLFormElement>) => {
  e.preventDefault();
  fn(new FormData(e.currentTarget));
};
const str = (d: FormData, k: string) => String(d.get(k) ?? '');
const num = (d: FormData, k: string) => Number(d.get(k));
function ErrorText({ error }: { error: unknown }) {
  return error ? (
    <p role="alert" className="error">
      {error instanceof Error ? error.message : 'Ошибка запроса'}
    </p>
  ) : null;
}
const useProfiles = () =>
  useQuery({
    queryKey: ['catalog', 'profiles'],
    queryFn: async () => unwrap(await api.GET('/api/profiles')),
  });

export function CatalogAdmin() {
  const cache = useQueryClient();
  const templates = useQuery({
    queryKey: ['catalog', 'templates'],
    queryFn: async () => unwrap(await api.GET('/api/admin/template-versions')),
  });
  const profiles = useProfiles();
  const policies = useQuery({
    queryKey: ['catalog', 'policies'],
    queryFn: async () => unwrap(await api.GET('/api/admin/permission-policies')),
  });
  const users = useQuery({
    queryKey: ['catalog', 'teachers'],
    queryFn: async () =>
      unwrap(await api.GET('/api/admin/users', { params: { query: { limit: 100 } } })),
  });
  const [teacher, setTeacher] = useState('');
  const [notice, setNotice] = useState('');
  const current = useQuery({
    queryKey: ['catalog', 'assignment', teacher],
    enabled: !!teacher,
    queryFn: async () =>
      unwrap(
        await api.GET('/api/teachers/{teacher_id}/permissions', {
          params: { path: { teacher_id: teacher } },
        }),
      ),
  });
  const saved = () => {
    cache.invalidateQueries({ queryKey: ['catalog'] });
    setNotice('Сохранено.');
  };
  const template = useMutation({
    mutationFn: async (d: FormData) =>
      unwrap(
        await api.POST('/api/admin/template-versions', {
          body: {
            name: str(d, 'name'),
            version_label: str(d, 'version_label'),
            runtime_kind: str(d, 'runtime_kind') as 'LXC' | 'QEMU',
            guest_family: str(d, 'guest_family') as 'LINUX' | 'WINDOWS',
          },
        }),
      ),
    onSuccess: saved,
  });
  const profile = useMutation({
    mutationFn: async (d: FormData) =>
      unwrap(
        await api.POST('/api/admin/profile-versions', {
          body: {
            name: str(d, 'name'),
            template_version_id: str(d, 'template_version_id'),
            memory_mib: num(d, 'memory_mib'),
            vcpu: num(d, 'vcpu'),
            cpu_millicredits: num(d, 'cpu_millicredits'),
            disk_gib: num(d, 'disk_gib'),
            network_mode: str(d, 'network_mode') as 'ISOLATED' | 'GROUP_LAN',
            internet_enabled: d.has('internet_enabled'),
          },
        }),
      ),
    onSuccess: saved,
  });
  const policy = useMutation({
    mutationFn: async (d: FormData) =>
      unwrap(
        await api.POST('/api/admin/permission-policies', {
          body: {
            name: str(d, 'name'),
            permissions: Object.fromEntries(
              Object.keys(names).map((k) => [k, d.has(k)]),
            ) as Policy['permissions'],
            limits: Object.fromEntries(
              Object.keys(limitNames).map((k) => [k, num(d, k)]),
            ) as Policy['limits'],
            demo_profile_ids: [str(d, 'demo_profile_id')],
          },
        }),
      ),
    onSuccess: saved,
  });
  const assign = useMutation({
    mutationFn: async (d: FormData) =>
      unwrap(
        await api.PUT('/api/admin/teachers/{teacher_id}/policy', {
          params: { path: { teacher_id: teacher } },
          body: {
            revision_id: str(d, 'revision_id'),
            expected_version: current.data!.assignment_version,
          },
        }),
      ),
    onSuccess: saved,
    onError: () => cache.invalidateQueries({ queryKey: ['catalog', 'assignment', teacher] }),
  });
  return (
    <>
      <header className="page-heading">
        <div>
          <span className="eyebrow">Администрирование</span>
          <h1>Профили и права</h1>
          <p className="muted">Настройте каталог, затем назначьте преподавателю политику.</p>
        </div>
      </header>
      <p className="footnote">
        Сейчас это подготовка конфигураций. Импорт образов и запуск машин на Proxmox ещё не
        подключены. Каталог показывает первые 100 записей.
      </p>
      {notice && (
        <p role="status" className="success">
          {notice}
        </p>
      )}
      <ErrorText error={templates.error || profiles.error || policies.error || users.error} />
      <div className="catalog-grid">
        <section className="panel">
          <h2>1. Версия шаблона</h2>
          <p className="muted">
            Описание базового образа. Для другой версии создайте новую запись.
          </p>
          <form onSubmit={submit((d) => template.mutate(d))}>
            <label>
              Название шаблона
              <input name="name" required maxLength={120} placeholder="Debian для практикума" />
            </label>
            <label>
              Версия образа
              <input name="version_label" required maxLength={64} placeholder="2026.09" />
            </label>
            <div className="field-grid">
              <label>
                Тип машины
                <select name="runtime_kind">
                  <option value="LXC">LXC</option>
                  <option value="QEMU">QEMU VM</option>
                </select>
              </label>
              <label>
                Операционная система
                <select name="guest_family">
                  <option value="LINUX">Linux</option>
                  <option value="WINDOWS">Windows (только VM)</option>
                </select>
              </label>
            </div>
            <button className="primary" disabled={template.isPending}>
              Добавить шаблон
            </button>
            <ErrorText error={template.error} />
          </form>
          <ul className="catalog-list">
            {templates.data?.map((t) => (
              <li key={t.id}>
                {t.name} · {t.version_label}{' '}
                <small>{t.runtime_kind} · образ на сервере ещё не проверен</small>
              </li>
            ))}
          </ul>
        </section>
        <section className="panel">
          <h2>2. Профиль машины</h2>
          <form onSubmit={submit((d) => profile.mutate(d))}>
            <label>
              Название профиля
              <input name="name" required maxLength={120} placeholder="Linux · 512 MiB" />
            </label>
            <label>
              Версия шаблона
              <select name="template_version_id" required defaultValue="">
                <option value="" disabled>
                  Выберите шаблон
                </option>
                {templates.data?.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name} · {t.version_label} · {t.runtime_kind}
                  </option>
                ))}
              </select>
            </label>
            <div className="field-grid">
              <label>
                RAM, MiB
                <input
                  name="memory_mib"
                  type="number"
                  min={128}
                  max={1048576}
                  defaultValue={512}
                  required
                />
              </label>
              <label>
                Виртуальные CPU
                <input name="vcpu" type="number" min={1} max={128} defaultValue={1} required />
              </label>
              <label>
                CPU, милликредиты
                <input
                  name="cpu_millicredits"
                  type="number"
                  min={1}
                  max={128000}
                  defaultValue={1000}
                  required
                />
              </label>
              <label>
                Диск, GiB
                <input
                  name="disk_gib"
                  type="number"
                  min={1}
                  max={1048576}
                  defaultValue={10}
                  required
                />
              </label>
            </div>
            <small>
              1000 милликредитов = 1 учётный CPU-кредит. Это не измерение загрузки процессора.
            </small>
            <label>
              Сеть студентов
              <select name="network_mode">
                <option value="ISOLATED">Изолированы друг от друга</option>
                <option value="GROUP_LAN">Общая сеть внутри окружения</option>
              </select>
            </label>
            <label className="check-label">
              <input name="internet_enabled" type="checkbox" />
              Доступ в Интернет
            </label>
            <button className="primary" disabled={profile.isPending || !templates.data?.length}>
              Сохранить профиль
            </button>
            <ErrorText error={profile.error} />
          </form>
          <ul className="catalog-list">
            {profiles.data?.map((p) => (
              <li key={p.id}>
                {p.name}
                <small>
                  {p.runtime_kind} · {p.memory_mib} MiB · {p.vcpu} vCPU · {p.disk_gib} GiB ·{' '}
                  {p.internet_enabled ? 'Интернет' : 'Без Интернета'}
                </small>
              </li>
            ))}
          </ul>
        </section>
      </div>
      <section className="panel">
        <h2>3. Политика преподавателя</h2>
        <p className="muted">
          Сохранённые политики неизменяемы. Для новых правил создайте новую и назначьте её
          преподавателю. Ноль означает запрет, а не отсутствие лимита.
        </p>
        <form onSubmit={submit((d) => policy.mutate(d))}>
          <label>
            Название политики
            <input name="name" required maxLength={120} placeholder="Практикум Linux" />
          </label>
          <div className="permission-grid">
            {Object.entries(names).map(([key, label]) => (
              <label key={key} className="check-label">
                <input type="checkbox" name={key} />
                {label}
              </label>
            ))}
          </div>
          <p className="muted">Snapshots доступны только администратору. Backup отключён.</p>
          <div className="field-grid">
            {Object.entries(limitNames).map(([key, label]) => (
              <label key={key}>
                {label}
                <input
                  type="number"
                  name={key}
                  min={0}
                  max={1000000000}
                  defaultValue={0}
                  required
                />
              </label>
            ))}
          </div>
          <label>
            Разрешённый демонстрационный профиль
            <select name="demo_profile_id" required defaultValue="">
              <option value="" disabled>
                Выберите профиль для демо
              </option>
              {profiles.data?.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} · {p.runtime_kind}
                </option>
              ))}
            </select>
          </label>
          <small>
            Явное разрешение демо не разрешает студентам тот же тип VM. Настройки сети должны быть
            разрешены политикой.
          </small>
          <button className="primary" disabled={policy.isPending || !profiles.data?.length}>
            Сохранить политику
          </button>
          <ErrorText error={policy.error} />
        </form>
      </section>
      <section className="panel">
        <h2>4. Назначить политику</h2>
        <form onSubmit={submit((d) => assign.mutate(d))}>
          <label>
            Преподаватель
            <select value={teacher} onChange={(e) => setTeacher(e.target.value)} required>
              <option value="">Выберите преподавателя</option>
              {users.data
                ?.filter((u) => u.roles.includes('TEACHER'))
                .map((u) => (
                  <option value={u.id} key={u.id}>
                    {u.display_name} · {u.username}
                  </option>
                ))}
            </select>
          </label>
          <label>
            Политика
            <select name="revision_id" required defaultValue="">
              <option value="" disabled>
                Выберите политику
              </option>
              {policies.data?.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <button className="primary" disabled={assign.isPending || !current.data}>
            Назначить
          </button>
          <ErrorText error={assign.error || current.error} />
        </form>
        {current.data && (
          <p className="footnote">
            Версия назначения: {current.data.assignment_version}. Индивидуальное разрешение
            создавать группы из раздела «Пользователи» имеет приоритет над политикой.
          </p>
        )}
      </section>
    </>
  );
}

export function EnvironmentPanel({ groupId, canCreate }: { groupId: string; canCreate: boolean }) {
  const cache = useQueryClient();
  const profiles = useProfiles();
  const [requestId, setRequestId] = useState(() => crypto.randomUUID());
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const environments = useQuery({
    queryKey: ['environments', groupId],
    queryFn: async () =>
      unwrap(await api.GET('/api/environments', { params: { query: { group_id: groupId } } })),
  });
  const body = (d: FormData): Draft => ({
    name: str(d, 'name'),
    group_id: groupId,
    profile_version_id: str(d, 'profile_version_id'),
    demo_profile_version_id: str(d, 'demo_profile_version_id'),
    request_id: requestId,
  });
  const calculate = useMutation({
    mutationFn: async (d: FormData) =>
      unwrap(await api.POST('/api/environments/estimate', { body: body(d) })),
    onSuccess: setEstimate,
  });
  const create = useMutation({
    mutationFn: async (d: FormData) =>
      unwrap(await api.POST('/api/environments', { body: body(d) })),
    onSuccess: () => {
      cache.invalidateQueries({ queryKey: ['environments', groupId] });
      setRequestId(crypto.randomUUID());
      setEstimate(null);
    },
  });
  return (
    <section className="panel">
      <h2>Учебные окружения</h2>
      <p className="muted">
        Сохраните конфигурацию занятия. Запуск на сервере появится после подключения Proxmox.
      </p>
      <ErrorText error={profiles.error || environments.error} />
      {canCreate && (
        <form
          onChange={() => {
            setEstimate(null);
            setRequestId(crypto.randomUUID());
          }}
          onSubmit={(e) => {
            e.preventDefault();
            const d = new FormData(e.currentTarget);
            if ((e.nativeEvent as SubmitEvent).submitter?.getAttribute('value') === 'create')
              create.mutate(d);
            else calculate.mutate(d);
          }}
        >
          <label>
            Название окружения
            <input name="name" required maxLength={120} placeholder="Основы Linux" />
          </label>
          <div className="field-grid">
            <label>
              Машина студента
              <select name="profile_version_id" required defaultValue="">
                <option value="" disabled>
                  Выберите разрешённый профиль
                </option>
                {profiles.data
                  ?.filter((p) => p.student_allowed)
                  .map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name} · {p.runtime_kind}
                    </option>
                  ))}
              </select>
            </label>
            <label>
              Демонстрационная машина
              <select name="demo_profile_version_id" required defaultValue="">
                <option value="" disabled>
                  Выберите демо
                </option>
                {profiles.data
                  ?.filter((p) => p.demo_allowed)
                  .map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name} · {p.runtime_kind}
                    </option>
                  ))}
              </select>
            </label>
          </div>
          {!profiles.data?.some((p) => p.student_allowed) && (
            <p className="muted">
              Попросите администратора назначить политику и разрешённые профили.
            </p>
          )}
          <div className="row-actions">
            <button
              type="submit"
              value="estimate"
              disabled={calculate.isPending || create.isPending}
            >
              Рассчитать ресурсы
            </button>
            <button
              type="submit"
              value="create"
              className="primary"
              disabled={calculate.isPending || create.isPending}
            >
              Создать окружение
            </button>
          </div>
          <ErrorText error={calculate.error || create.error} />
          {estimate && (
            <div className="resource-estimate">
              <h3>Расчёт для {estimate.student_count} студентов и одной демо</h3>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Машины</th>
                      <th>RAM, MiB</th>
                      <th>vCPU</th>
                      <th>Диски, GiB</th>
                      <th>RAM VM на диске, GiB</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(['students', 'demo', 'total'] as const).map((k) => (
                      <tr key={k}>
                        <td>{{ students: 'Студенты', demo: 'Демо', total: 'Всего' }[k]}</td>
                        <td>{estimate[k].memory_mib}</td>
                        <td>{estimate[k].vcpu}</td>
                        <td>{(estimate[k].disk_bytes / 2 ** 30).toFixed(1)}</td>
                        <td>{(estimate[k].hibernation_bytes / 2 ** 30).toFixed(2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p>CPU-бюджет: {estimate.total.cpu_millicredits / 1000} кредитов.</p>
              <p className={estimate.within_per_environment_limits ? 'success' : 'error'}>
                {estimate.within_per_environment_limits
                  ? 'Одно окружение укладывается в заданные лимиты.'
                  : `Превышены лимиты: ${estimate.violations.map((v) => limitNames[v as keyof typeof limitNames] ?? v).join(', ')}`}
              </p>
              <p className="footnote">
                Свободные ресурсы сервера и другие занятия ещё не проверены. Бронь не создана.
                Сохранённая RAM — минимальный объём; служебные расходы и запас storage будут
                учитываться при допуске к запуску.
              </p>
            </div>
          )}
        </form>
      )}
      <ul className="catalog-list">
        {environments.data?.map((e) => (
          <li key={e.id}>
            <strong>{e.name}</strong>
            <span className="pill">Подготовлено</span>
            <small>Профили закреплены. Машины ещё не созданы, ресурсы не зарезервированы.</small>
          </li>
        ))}
      </ul>
    </section>
  );
}
