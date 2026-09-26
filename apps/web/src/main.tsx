import { StrictMode, useState, type FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import {
  BrowserRouter,
  Link,
  NavLink,
  Route,
  Routes,
  useNavigate,
  useParams,
} from 'react-router-dom';
import { QueryClient, QueryClientProvider, useMutation, useQuery } from '@tanstack/react-query';
import { api, ApiError, setCsrfToken, unwrap } from '@lab/client-sdk';
import type { components } from '@lab/shared-types';
import '@fontsource/roboto/cyrillic-400.css';
import '@fontsource/roboto/latin-400.css';
import '@fontsource/roboto/cyrillic-500.css';
import '@fontsource/roboto/latin-500.css';
import '@fontsource/roboto/cyrillic-700.css';
import '@fontsource/roboto/latin-700.css';
import '@fontsource/unbounded/cyrillic-600.css';
import '@fontsource/unbounded/latin-600.css';
import '@fontsource/unbounded/cyrillic-700.css';
import '@fontsource/unbounded/latin-700.css';
import './style.css';
import { CatalogAdmin, EnvironmentPanel } from './catalog';

type Session = components['schemas']['SessionView'];
type Group = components['schemas']['GroupView'];
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, staleTime: 15_000 }, mutations: { retry: false } },
});
const message = (error: unknown) =>
  error instanceof Error ? error.message : 'Сервис недоступен. Попробуйте позже.';
function ErrorNotice({ error }: { error: unknown }) {
  return error ? (
    <p className="error" role="alert">
      {message(error)}
    </p>
  ) : null;
}
function Loading() {
  return (
    <p role="status" className="muted">
      Загрузка…
    </p>
  );
}
function Brand() {
  return (
    <Link to="/" className="brand">
      <svg className="brand-mark" viewBox="0 0 48 48" aria-hidden="true">
        <path d="M5 13 24 3l19 10-19 10Z" fill="currentColor" />
        <path
          d="m5 23 19 10 19-10M5 33l19 10 19-10"
          fill="none"
          stroke="currentColor"
          strokeWidth="4"
          strokeLinejoin="round"
        />
      </svg>
      <span>
        Lab Manager<small>Учебная лаборатория</small>
      </span>
    </Link>
  );
}
function submit(action: (data: FormData) => void) {
  return (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    action(new FormData(event.currentTarget));
  };
}
const field = (data: FormData, name: string) => String(data.get(name) ?? '');

function Auth() {
  const [mode, setMode] = useState<'login' | 'register' | 'reset'>('login');
  const [notice, setNotice] = useState('');
  const mutation = useMutation({
    mutationFn: async (data: FormData) => {
      if (mode === 'register') {
        unwrap(
          await api.POST('/api/auth/register', {
            body: {
              username: field(data, 'username'),
              display_name: field(data, 'display_name'),
              password: field(data, 'password'),
            },
          }),
        );
        setMode('login');
        setNotice('Аккаунт создан. Войдите с вашим логином и паролем.');
      } else if (mode === 'reset') {
        unwrap(
          await api.POST('/api/auth/reset-password', {
            body: { token: field(data, 'token'), password: field(data, 'password') },
          }),
        );
        setMode('login');
        setNotice('Пароль изменён. Теперь можно войти.');
      } else {
        const session = unwrap(
          await api.POST('/api/auth/login', {
            body: { username: field(data, 'username'), password: field(data, 'password') },
          }),
        );
        queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== 'session' });
        setCsrfToken(session.csrf_token);
        queryClient.setQueryData(['session'], session);
      }
    },
  });
  const changeMode = (value: typeof mode) => {
    setMode(value);
    setNotice('');
    mutation.reset();
  };
  return (
    <div className="auth-layout">
      <section className="auth-story">
        <Brand />
        <div>
          <span className="eyebrow">Место для практики</span>
          <h1>
            Ваша лаборатория.
            <br />
            <em>В одном окне.</em>
          </h1>
          <p>Присоединяйтесь к учебной группе. Доступ к лабораторным работам будет здесь.</p>
        </div>
        <span className="story-foot">LAB MANAGER / УЧЕБНЫЕ ГРУППЫ</span>
      </section>
      <main className="auth-main">
        <div className="auth-card">
          <span className="eyebrow">Добро пожаловать</span>
          <h2>
            {mode === 'login'
              ? 'Войти в лабораторию'
              : mode === 'register'
                ? 'Аккаунт студента'
                : 'Восстановить доступ'}
          </h2>
          <p className="muted">
            {mode === 'login'
              ? 'Введите данные вашего аккаунта.'
              : mode === 'register'
                ? 'После регистрации введите код, который выдал преподаватель.'
                : 'Получите у администратора одноразовый код восстановления. Он действует 15 минут.'}
          </p>
          <form key={mode} onSubmit={submit((data) => mutation.mutate(data))}>
            {mode === 'reset' ? (
              <label>
                Код восстановления
                <input name="token" required autoComplete="off" minLength={32} maxLength={128} />
              </label>
            ) : (
              <label>
                Логин
                <input
                  name="username"
                  aria-label="Логин"
                  aria-describedby="username-hint"
                  required
                  autoComplete="username"
                  minLength={3}
                  maxLength={64}
                  pattern="[a-zA-Z0-9_.\-]+"
                  placeholder="ivan.petrov"
                />
                <small id="username-hint">
                  Латинские буквы, цифры, точка, дефис и подчёркивание.
                </small>
              </label>
            )}
            {mode === 'register' && (
              <label>
                Имя и фамилия
                <input
                  name="display_name"
                  required
                  autoComplete="name"
                  maxLength={120}
                  placeholder="Иван Петров"
                />
              </label>
            )}
            <label>
              {mode === 'reset' ? 'Новый пароль' : 'Пароль'}
              <input
                name="password"
                aria-label={mode === 'reset' ? 'Новый пароль' : 'Пароль'}
                aria-describedby={mode === 'login' ? undefined : 'password-hint'}
                type="password"
                required
                minLength={mode === 'login' ? 1 : 12}
                maxLength={128}
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              />
              {mode !== 'login' && <small id="password-hint">Не менее 12 символов.</small>}
            </label>
            <ErrorNotice error={mutation.error} />
            {notice && (
              <p role="status" className="success">
                {notice}
              </p>
            )}
            <button className="primary wide" disabled={mutation.isPending}>
              {mutation.isPending
                ? 'Подождите…'
                : mode === 'login'
                  ? 'Войти →'
                  : mode === 'register'
                    ? 'Создать аккаунт'
                    : 'Сохранить пароль'}
            </button>
          </form>
          <div className="auth-actions">
            {mode === 'login' ? (
              <>
                <button className="text-button" onClick={() => changeMode('register')}>
                  Регистрация студента
                </button>
                <button className="text-button" onClick={() => changeMode('reset')}>
                  Забыли пароль?
                </button>
              </>
            ) : (
              <button className="text-button" onClick={() => changeMode('login')}>
                ← Вернуться ко входу
              </button>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}

function GroupList({ session }: { session: Session }) {
  const navigate = useNavigate();
  const [after, setAfter] = useState<string | undefined>();
  const groups = useQuery({
    queryKey: ['groups', after],
    queryFn: async () =>
      unwrap(await api.GET('/api/groups', { params: { query: { limit: 50, after } } })),
  });
  const create = useMutation({
    mutationFn: async (data: FormData) =>
      unwrap(await api.POST('/api/groups', { body: { name: field(data, 'name') } })),
    onSuccess: (group) => {
      queryClient.invalidateQueries({ queryKey: ['groups'] });
      navigate(`/groups/${group.id}`);
    },
  });
  const join = useMutation({
    mutationFn: async (data: FormData) =>
      unwrap(
        await api.POST('/api/groups/join', {
          body: { code: field(data, 'code').trim().toUpperCase(), confirm: true },
        }),
      ),
    onSuccess: (group) => {
      queryClient.invalidateQueries({ queryKey: ['groups'] });
      navigate(`/groups/${group.id}`);
    },
  });
  return (
    <>
      <header className="page-heading">
        <div>
          <span className="eyebrow">Рабочее пространство</span>
          <h1>Учебные группы</h1>
          <p className="muted">
            {session.user.roles.includes('ADMIN')
              ? 'Группы лаборатории и их участники.'
              : 'Ваши группы и доступ к учебным материалам.'}
          </p>
        </div>
        <span className="pill">
          {session.user.roles.includes('TEACHER')
            ? 'Преподаватель'
            : session.user.roles.includes('ADMIN')
              ? 'Администратор'
              : 'Студент'}
        </span>
      </header>
      <div className="action-grid">
        {session.can_create_groups && (
          <section className="panel">
            <h2>Создать группу</h2>
            <p className="muted">Поделитесь коротким кодом со студентами.</p>
            <form className="inline-form" onSubmit={submit((data) => create.mutate(data))}>
              <label>
                Название группы
                <input name="name" required maxLength={120} placeholder="Например, ИС-21" />
              </label>
              <button className="primary" disabled={create.isPending}>
                Создать
              </button>
            </form>
            <ErrorNotice error={create.error} />
          </section>
        )}
        {session.user.roles.includes('STUDENT') && (
          <section className="panel">
            <h2>Присоединиться к группе</h2>
            <p className="muted">Введите код преподавателя один раз.</p>
            <form className="inline-form" onSubmit={submit((data) => join.mutate(data))}>
              <label>
                Код группы
                <input
                  name="code"
                  required
                  minLength={6}
                  maxLength={6}
                  className="code-input"
                  placeholder="ABC234"
                  autoComplete="off"
                />
              </label>
              <button className="primary" disabled={join.isPending}>
                Вступить
              </button>
            </form>
            <ErrorNotice error={join.error} />
          </section>
        )}
      </div>
      <section className="group-section">
        <h2>Доступные группы</h2>
        {groups.isPending && <Loading />}
        <ErrorNotice error={groups.error} />
        {groups.data?.length === 0 && (
          <div className="empty">
            <span className="empty-symbol">▦</span>
            <h3>Здесь появятся ваши группы</h3>
            <p className="muted">
              {session.can_create_groups
                ? 'Создайте первую группу и пригласите студентов.'
                : 'Если вы студент, запросите код у преподавателя.'}
            </p>
          </div>
        )}
        <div className="cards">
          {groups.data?.map((group) => (
            <Link className="group-card" to={`/groups/${group.id}`} key={group.id}>
              <div className="group-icon">{group.name.slice(0, 2).toUpperCase()}</div>
              <span className="pill subtle">
                {group.join_enabled ? 'Приём открыт' : 'Приём закрыт'}
              </span>
              <h3>{group.name}</h3>
              <p className="muted">
                {group.member_count != null
                  ? `${group.member_count} участников`
                  : 'Вы участник группы'}
              </p>
              <span className="card-link">
                Открыть группу <span>↗</span>
              </span>
            </Link>
          ))}
        </div>
        <Pagination
          count={groups.data?.length ?? 0}
          after={after}
          next={() => setAfter(groups.data?.at(-1)?.id)}
          first={() => setAfter(undefined)}
        />
      </section>
      <p className="footnote">
        Сейчас доступны учётные записи и группы. Учебные машины и подключение через браузер появятся
        на следующем этапе.
      </p>
    </>
  );
}

function Pagination({
  count,
  after,
  next,
  first,
}: {
  count: number;
  after?: string;
  next: () => void;
  first: () => void;
}) {
  return (
    <div className="pagination">
      {after && <button onClick={first}>В начало</button>}
      {count === 50 && <button onClick={next}>Следующие 50 →</button>}
    </div>
  );
}

function GroupDetail({ session }: { session: Session }) {
  const { id = '' } = useParams();
  const [after, setAfter] = useState<string | undefined>();
  const group = useQuery({
    queryKey: ['group', id],
    queryFn: async () =>
      unwrap(await api.GET('/api/groups/{group_id}', { params: { path: { group_id: id } } })),
  });
  const managed = group.data?.join_code != null;
  const members = useQuery({
    queryKey: ['members', id, after],
    enabled: managed,
    queryFn: async () =>
      unwrap(
        await api.GET('/api/groups/{group_id}/members', {
          params: { path: { group_id: id }, query: { after, limit: 50 } },
        }),
      ),
  });
  const refresh = (data: Group) => {
    queryClient.setQueryData(['group', id], data);
    queryClient.invalidateQueries({ queryKey: ['groups'] });
  };
  const toggle = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.PATCH('/api/groups/{group_id}', {
          params: { path: { group_id: id } },
          body: { expected_version: group.data!.version, join_enabled: !group.data!.join_enabled },
        }),
      ),
    onSuccess: refresh,
    onError: () => queryClient.invalidateQueries({ queryKey: ['group', id] }),
  });
  const rotate = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST('/api/groups/{group_id}/join-code/regenerate', {
          params: { path: { group_id: id } },
          body: { expected_version: group.data!.version },
        }),
      ),
    onSuccess: refresh,
    onError: () => queryClient.invalidateQueries({ queryKey: ['group', id] }),
  });
  if (group.isPending) return <Loading />;
  if (!group.data) return <ErrorNotice error={group.error} />;
  return (
    <>
      <Link className="back-link" to="/">
        ← Все группы
      </Link>
      <header className="page-heading">
        <div>
          <span className="eyebrow">Учебная группа</span>
          <h1>{group.data.name}</h1>
        </div>
        <span className="pill">{group.data.join_enabled ? 'Приём открыт' : 'Приём закрыт'}</span>
      </header>
      {managed && (
        <section className="panel join-panel">
          <div>
            <h2>Код приглашения</h2>
            <p className="muted">Студент вводит его в своём аккаунте.</p>
            <output className="join-code">{group.data.join_code}</output>
          </div>
          <div className="button-stack">
            <button onClick={() => toggle.mutate()} disabled={toggle.isPending || rotate.isPending}>
              {group.data.join_enabled ? 'Закрыть приём' : 'Открыть приём'}
            </button>
            <button
              onClick={() => {
                if (
                  window.confirm(
                    'Заменить код группы? Старый код сразу перестанет работать. Участники останутся в группе.',
                  )
                )
                  rotate.mutate();
              }}
              disabled={rotate.isPending || toggle.isPending}
            >
              Заменить код
            </button>
            <small>Смена кода не удаляет участников.</small>
          </div>
          <ErrorNotice error={toggle.error || rotate.error} />
        </section>
      )}
      {managed && (
        <section className="panel">
          <h2>Участники</h2>
          {members.isPending && <Loading />}
          <ErrorNotice error={members.error} />
          {members.data?.length === 0 && (
            <p className="muted">Пока никто не присоединился. Передайте студентам код группы.</p>
          )}
          <div className="member-list">
            {members.data?.map((user) => (
              <div className="member-row" key={user.id}>
                <span className="avatar">{user.display_name[0]}</span>
                <div>
                  <strong>{user.display_name}</strong>
                  <small>{user.username}</small>
                </div>
                <span className="pill subtle">Студент</span>
              </div>
            ))}
          </div>
          <Pagination
            count={members.data?.length ?? 0}
            after={after}
            next={() => setAfter(members.data?.at(-1)?.id)}
            first={() => setAfter(undefined)}
          />
        </section>
      )}
      {managed ? (
        <EnvironmentPanel groupId={id} canCreate={session.user.roles.includes('TEACHER')} />
      ) : (
        <section className="empty">
          <span className="empty-symbol">⌘</span>
          <h2>Учебные окружения ещё не подключены</h2>
          <p className="muted">
            Группа сохранена. Создание машин и проведение занятий находятся в разработке.
          </p>
        </section>
      )}
    </>
  );
}

function Admin() {
  const [after, setAfter] = useState<string | undefined>();
  const [resetToken, setResetToken] = useState<{ name: string; token: string } | null>(null);
  const users = useQuery({
    queryKey: ['users', after],
    queryFn: async () =>
      unwrap(await api.GET('/api/admin/users', { params: { query: { after, limit: 50 } } })),
  });
  const grant = useMutation({
    mutationFn: async ({ id, allowed }: { id: string; allowed: boolean }) =>
      unwrap(
        await api.PUT('/api/admin/users/{user_id}/teacher', {
          params: { path: { user_id: id } },
          body: { can_create_groups: allowed },
        }),
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['users'] }),
  });
  const reset = useMutation({
    mutationFn: async ({ id, name }: { id: string; name: string }) => {
      const result = unwrap(
        await api.POST('/api/admin/users/{user_id}/password-reset', {
          params: { path: { user_id: id } },
        }),
      );
      setResetToken({ name, token: result.token });
    },
  });
  return (
    <>
      <header className="page-heading">
        <div>
          <span className="eyebrow">Администрирование</span>
          <h1>Пользователи</h1>
          <p className="muted">Назначайте преподавателей и помогайте восстановить доступ.</p>
        </div>
      </header>
      <ErrorNotice error={users.error || grant.error || reset.error} />
      {users.isPending && <Loading />}
      {resetToken && (
        <section className="panel">
          <h2>Восстановление: {resetToken.name}</h2>
          <p>
            Передайте код лично после проверки личности. Действует 15 минут; прежние сессии
            отозваны.
          </p>
          <label>
            Одноразовый код
            <input readOnly value={resetToken.token} onFocus={(event) => event.target.select()} />
          </label>
          <button onClick={() => setResetToken(null)}>Скрыть код</button>
        </section>
      )}
      <section className="panel">
        <div className="member-list">
          {users.data?.map((user) => (
            <div className="member-row admin-row" key={user.id}>
              <span className="avatar">{user.display_name[0]}</span>
              <div className="member-name">
                <strong>{user.display_name}</strong>
                <small>
                  {user.username} ·{' '}
                  {user.roles
                    .map(
                      (role) =>
                        ({ ADMIN: 'Администратор', TEACHER: 'Преподаватель', STUDENT: 'Студент' })[
                          role
                        ],
                    )
                    .join(', ')}
                </small>
              </div>
              <div className="row-actions">
                <button
                  disabled={grant.isPending}
                  onClick={() => grant.mutate({ id: user.id, allowed: true })}
                >
                  {user.roles.includes('TEACHER') ? 'Разрешить группы' : 'Назначить преподавателем'}
                </button>
                {user.roles.includes('TEACHER') && (
                  <button
                    disabled={grant.isPending}
                    onClick={() => grant.mutate({ id: user.id, allowed: false })}
                  >
                    Запретить новые группы
                  </button>
                )}
                <button
                  disabled={reset.isPending}
                  onClick={() => {
                    if (
                      window.confirm(
                        `Выдать код восстановления для ${user.display_name}? Текущие сессии будут отозваны.`,
                      )
                    )
                      reset.mutate({ id: user.id, name: user.display_name });
                  }}
                >
                  Восстановить доступ
                </button>
              </div>
            </div>
          ))}
        </div>
        <Pagination
          count={users.data?.length ?? 0}
          after={after}
          next={() => setAfter(users.data?.at(-1)?.id)}
          first={() => setAfter(undefined)}
        />
      </section>
      <p className="footnote">
        Здесь настраивается создание групп. Права на VM, LXC, сети и ресурсные квоты будут доступны
        вместе с управлением окружениями.
      </p>
    </>
  );
}

function App() {
  const session = useQuery({
    queryKey: ['session'],
    queryFn: async () => {
      const result = await api.GET('/api/auth/me');
      if (result.response.status === 401) {
        setCsrfToken(null);
        return null;
      }
      const value = unwrap(result);
      setCsrfToken(value.csrf_token);
      return value;
    },
  });
  const logout = useMutation({
    mutationFn: async () => unwrap(await api.POST('/api/auth/logout')),
    onSuccess: () => {
      setCsrfToken(null);
      queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== 'session' });
      queryClient.setQueryData(['session'], null);
    },
  });
  if (session.isPending)
    return (
      <div className="boot">
        <Brand />
        <Loading />
      </div>
    );
  if (session.error)
    return (
      <div className="boot">
        <Brand />
        <ErrorNotice error={session.error} />
        <button onClick={() => session.refetch()}>Повторить подключение</button>
      </div>
    );
  if (!session.data) return <Auth />;
  const value = session.data;
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Brand />
        <span className="nav-label">ЛАБОРАТОРИЯ</span>
        <nav>
          <NavLink to="/" end>
            ▦ <span>Учебные группы</span>
          </NavLink>
          {value.user.roles.includes('ADMIN') && (
            <NavLink to="/admin">
              ☷ <span>Пользователи</span>
            </NavLink>
          )}
          {value.user.roles.includes('ADMIN') && (
            <NavLink to="/admin/catalog">
              ◇ <span>Профили и права</span>
            </NavLink>
          )}
        </nav>
        <div className="sidebar-bottom">
          <span className="avatar">{value.user.display_name[0]}</span>
          <strong>{value.user.display_name}</strong>
          <small>{value.user.username}</small>
          <button onClick={() => logout.mutate()} disabled={logout.isPending}>
            Выйти
          </button>
          <ErrorNotice error={logout.error} />
        </div>
      </aside>
      <main className="workspace">
        <div className="topline">
          <span>LAB MANAGER</span>
          <span>Учебное пространство</span>
        </div>
        <div className="page">
          <Routes>
            <Route path="/" element={<GroupList session={value} />} />
            <Route path="/groups/:id" element={<GroupDetail session={value} />} />
            {value.user.roles.includes('ADMIN') && <Route path="/admin" element={<Admin />} />}
            {value.user.roles.includes('ADMIN') && (
              <Route path="/admin/catalog" element={<CatalogAdmin />} />
            )}
            <Route
              path="*"
              element={
                <p>
                  Страница не найдена. <Link to="/">Вернуться к группам</Link>
                </p>
              }
            />
          </Routes>
        </div>
      </main>
    </div>
  );
}

// Expired sessions clear cached private data before showing another account.
queryClient.getQueryCache().subscribe((event) => {
  if (
    event.type === 'updated' &&
    event.action.type === 'success' &&
    event.query.queryKey[0] === 'session' &&
    event.query.state.data === null
  ) {
    queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== 'session' });
  }
  if (
    event.type === 'updated' &&
    event.action.type === 'error' &&
    event.action.error instanceof ApiError &&
    event.action.error.status === 401 &&
    event.query.queryKey[0] !== 'session'
  ) {
    setCsrfToken(null);
    queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== 'session' });
    queryClient.setQueryData(['session'], null);
  }
});
queryClient.getMutationCache().subscribe((event) => {
  if (
    event.type === 'updated' &&
    event.action.type === 'error' &&
    event.action.error instanceof ApiError &&
    event.action.error.status === 401
  ) {
    setCsrfToken(null);
    queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== 'session' });
    queryClient.setQueryData(['session'], null);
  }
});
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
