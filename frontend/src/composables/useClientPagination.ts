import { computed, ref, watch, type Ref } from 'vue'

export function useClientPagination<T>(items: Ref<T[]>) {
  const currentPage = ref(1)
  const pageSize = ref(10)
  const paginatedItems = computed(() => {
    const start = (currentPage.value - 1) * pageSize.value
    return items.value.slice(start, start + pageSize.value)
  })

  watch(pageSize, () => { currentPage.value = 1 })
  watch(
    () => items.value.length,
    (total) => {
      const lastPage = Math.max(1, Math.ceil(total / pageSize.value))
      if (currentPage.value > lastPage) currentPage.value = lastPage
    }
  )

  function resetPage() { currentPage.value = 1 }

  return { currentPage, pageSize, paginatedItems, resetPage }
}
