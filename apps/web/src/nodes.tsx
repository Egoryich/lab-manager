import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, unwrap } from '@lab/client-sdk';
import type { components } from '@lab/shared-types';

type Node = components['schemas']['NodeView'];

function ResourcePolicy({ node }: { node: Node }) {
  const cache = useQueryClient();
  const policy = useQuery({
    queryKey: ['node-resource-policy', node.id],
    queryFn: async () => {
      const result = await api.GET('/api/admin/nodes/{node_id}/resource-policy', {
        params: { path: { node_id: node.id } },
      });
      return result.response.status === 404 ? null : unwrap(result);
    },
  });
  const save = useMutation({
    mutationFn: async (form: FormData) =>
      unwrap(
        await api.PUT('/api/admin/nodes/{node_id}/resource-policy', {
          params: { path: { node_id: node.id } },
          body: {
            storage_name: String(form.get('storage_name')),
            host_reserve_mib: Number(form.get('host_reserve_mib')),
            infrastructure_reserve_mib: Number(form.get('infrastructure_reserve_mib')),
            safety_reserve_mib: Number(form.get('safety_reserve_mib')),
            cpu_millicredits_per_logical_cpu: 1000,
            storage_free_percent: 10,
            thin_metadata_limit_percent: 80,
            expected_version: policy.data?.version ?? 0,
          },
        }),
      ),
    onSuccess: (value) => cache.setQueryData(['node-resource-policy', node.id], value),
  });
  const selected = policy.data;
  const memoryMiB = node.host ? Math.ceil(node.host.memory_total_bytes / 2 ** 20) : 0;
  const candidate = node.storages?.filter((s) => s.active && s.backend === 'lvmthin') ?? [];
  return (
    <div className="resource-estimate">
      <h3>Резервы узла для будущих занятий</h3>
      <p className="muted">
        Администратор выбирает учебное хранилище и запас RAM. Свободные ресурсы будут считаться из
        свежего снимка сервера и уже забронированных занятий. Сохранение этой настройки не разрешает
        запуск машин.
      </p>
      {policy.isPending && <p>Загрузка политики…</p>}
      {policy.error && <p className="error">{policy.error.message}</p>}
      {!policy.isPending && !policy.error && candidate.length > 0 && (
        <form
          key={selected?.version ?? 0}
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate(new FormData(event.currentTarget));
          }}
        >
          <label>
            Учебное хранилище
            <select name="storage_name" defaultValue={selected?.storage_name ?? 'student-lvm'}>
              {candidate.map((s) => (
                <option key={s.name} value={s.name}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
          <div className="field-grid">
            <label>
              Запас RAM для Proxmox, MiB
              <input
                name="host_reserve_mib"
                type="number"
                min={0}
                max={1048576}
                defaultValue={
                  selected?.host_reserve_mib ?? Math.max(2048, Math.ceil(memoryMiB / 10))
                }
                required
              />
            </label>
            <label>
              Запас RAM для инфраструктуры, MiB
              <input
                name="infrastructure_reserve_mib"
                type="number"
                min={0}
                max={1048576}
                defaultValue={selected?.infrastructure_reserve_mib ?? 0}
                required
              />
            </label>
            <label>
              Аварийный запас RAM, MiB
              <input
                name="safety_reserve_mib"
                type="number"
                min={0}
                max={1048576}
                defaultValue={selected?.safety_reserve_mib ?? 1024}
                required
              />
            </label>
          </div>
          <small>Для диска всегда сохраняется минимум 10% свободного места.</small>
          <button type="submit" disabled={save.isPending}>
            Сохранить резервы
          </button>
          {save.error && (
            <p role="alert" className="error">
              {save.error.message}
            </p>
          )}
          {save.isSuccess && (
            <p role="status" className="success">
              Политика сохранена.
            </p>
          )}
        </form>
      )}
      {!policy.isPending && candidate.length === 0 && <p>Доступного LVM-thin хранилища нет.</p>}
    </div>
  );
}

const gib = (value: number) => (value / 2 ** 30).toFixed(1);

export function NodePanel() {
  const query = useQuery({
    queryKey: ['nodes'],
    queryFn: async () => unwrap(await api.GET('/api/admin/nodes')),
    refetchInterval: 15_000,
  });
  return (
    <section>
      <h1>Серверы лаборатории</h1>
      <p className="muted">
        Последние сведения с физических серверов. Предварительная вместимость для выбранного
        времени рассчитывается в окружении; запуск машин пока заблокирован.
      </p>
      {query.isPending && <p role="status">Загрузка…</p>}
      {query.error && (
        <p className="error" role="alert">
          Не удалось обновить сведения. Показанные ранее данные могли устареть.
        </p>
      )}
      {query.data?.length === 0 && <p>Серверы ещё не подключены.</p>}
      {query.data?.map((node) => (
        <article className="group-card" key={node.id}>
          <h2>{node.name}</h2>
          <p>
            <strong>
              {query.error
                ? 'Нет актуальных данных'
                : node.status === 'FRESH'
                  ? 'Данные актуальны'
                  : node.status === 'STALE'
                    ? 'Данные устарели'
                    : 'Данные пока не получены'}
            </strong>
          </p>
          <p>Снимок: {node.sampled_at ? new Date(node.sampled_at).toLocaleString('ru-RU') : '—'}</p>
          {node.host && (
            <dl>
              <dt>Логические CPU</dt>
              <dd>{node.host.logical_cpus}</dd>
              <dt>Память сервера</dt>
              <dd>{gib(node.host.memory_total_bytes)} GiB</dd>
              <dt>Свободная память в снимке</dt>
              <dd>{gib(node.host.memory_free_bytes)} GiB</dd>
              <dt>Хранилища / машины</dt>
              <dd>
                {node.storage_count} / {node.guest_count}
              </dd>
            </dl>
          )}
          {node.storages && node.storages.length > 0 && (
            <div className="resource-estimate">
              <h3>Хранилища</h3>
              <p className="muted">
                Наблюдаемые значения Proxmox. Свободное место здесь не означает доступный лимит для
                новых машин. PV — устройства, которые LVM связывает с пулом; их проверка для запуска
                занятий ещё не завершена.
              </p>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Имя</th>
                      <th>Тип</th>
                      <th>Всего</th>
                      <th>Свободно</th>
                      <th>Thin metadata</th>
                      <th>Томов найдено</th>
                      <th>PV по LVM</th>
                      <th>Проверка PV</th>
                    </tr>
                  </thead>
                  <tbody>
                    {node.storages.map((storage) => (
                      <tr key={storage.name}>
                        <td>{storage.name}</td>
                        <td>{storage.backend}</td>
                        <td>
                          {storage.total_bytes === null ? '—' : `${gib(storage.total_bytes)} GiB`}
                        </td>
                        <td>
                          {storage.available_bytes === null
                            ? '—'
                            : `${gib(storage.available_bytes)} GiB`}
                        </td>
                        <td>
                          {storage.thin_metadata_percent === null
                            ? '—'
                            : `${storage.thin_metadata_percent}%`}
                        </td>
                        <td>{storage.observed_volume_count ?? '—'}</td>
                        <td>{storage.physical_volumes?.join(', ') || '—'}</td>
                        <td>
                          {storage.physical_backing_reconciled === null
                            ? 'Нет данных'
                            : storage.physical_backing_reconciled
                              ? 'Завершена'
                              : 'Не завершена'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
          {node.network_bridges && (
            <div className="resource-estimate">
              <h3>Сетевые мосты</h3>
              <p className="muted">
                Подключения, обнаруженные Proxmox. Отсутствие физического порта не подтверждает
                изоляцию учебных машин.
              </p>
              {node.network_bridges.length === 0 ? (
                <p>Мосты не найдены.</p>
              ) : (
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Мост</th>
                        <th>Состояние</th>
                        <th>Подключённые порты</th>
                      </tr>
                    </thead>
                    <tbody>
                      {node.network_bridges.map((bridge) => (
                        <tr key={bridge.name}>
                          <td>{bridge.name}</td>
                          <td>{bridge.active ? 'Активен' : 'Неактивен'}</td>
                          <td>{bridge.ports.join(', ') || 'Нет'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
          {!node.admission_ready && (
            <p className="muted">Запуск занятий пока заблокирован проверками ресурсов и сети.</p>
          )}
          <ResourcePolicy node={node} />
          {node.status !== 'FRESH' && (
            <p className="muted">
              Нет подтверждения текущего состояния. Это не означает, что сервер выключен.
            </p>
          )}
        </article>
      ))}
    </section>
  );
}
